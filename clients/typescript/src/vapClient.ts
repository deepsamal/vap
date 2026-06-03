/**
 * Thin VAP client (TypeScript, fetch-based).
 *
 * Same surface as the Python binding: builds the `_meta.vap` payloads, speaks MCP
 * JSON-RPC 2.0 over HTTP to the VAP proxy, and parses verdicts. No verification
 * logic lives client-side. Designed to `tsc --noEmit` cleanly (Node 18+).
 *
 * HMAC signing uses Node's `crypto`. For a pure browser target, swap `sign()` for
 * SubtleCrypto.
 */

import { createHmac } from "node:crypto";

export const VAP_VERSION = "0.1" as const;
const SESSION_HEADER = "mcp-session-id";

export type VerdictKind = "served" | "clarify" | "downgraded" | "denied" | "unknown";

export type Sensitivity =
  | "reads"
  | "writes_data"
  | "writes_money"
  | "deletes"
  | "sends_external"
  | "grants_access";

export interface Principal {
  agent_id: string;
  auth?: string;
  delegated_by?: string;
}

export interface Scope {
  /** Public tool-name patterns (glob). Gates on tool IDENTITY only -- never on
   * argument values. Argument-level rules are deployment-local operator policy. */
  tools_allow: string[];
  tools_deny?: string[];
}

export interface Budget {
  /** Universal ceiling on number of tool calls. */
  max_calls?: number;
  /** Session hard stop (RFC 3339). */
  deadline?: string;
  /**
   * Extensible, unit-agnostic map of named meters -> max value. Well-known keys:
   * `tokens`, `usd_opcost` (operational cost), and tool-self-reported meters like
   * `disbursed_usd`. Add any custom unit your tools report; VAP only sums and
   * enforces ceilings (it never knows what a meter means).
   */
  limits?: Record<string, number>;
}

export interface ScopeCommitment {
  vap?: string;
  type?: "scope_commitment";
  session_id?: string;
  goal: string;
  scope: Scope;
  budget?: Budget;
  plan_digest?: string;
  principal: Principal;
  signature?: string;
}

export interface Intent {
  rationale: string;
  expected_effect: string;
  step?: number;
  sensitivity?: Sensitivity;
  reasoning_digest?: string;
}

export interface Verification {
  checks: string[];
  method: "static" | "static+policy" | "static+semantic" | "static+policy+semantic";
  risk_score?: number;
  semantic_invoked?: boolean;
  semantic_trigger?: string;
  confidence?: number;
  reason?: string;
}

export interface Clarification {
  question: string;
  schema?: Record<string, unknown>;
}

export interface VapResult {
  verdict: VerdictKind;
  verification: Verification;
  clarification?: Clarification;
  auditRef?: string;
  acceptedCommitmentDigest?: string;
  result?: Record<string, unknown>;
  raw: unknown;
}

interface VapVerdictPayload {
  verdict: VerdictKind;
  verification: Verification;
  clarification?: Clarification;
  audit_ref?: string;
  accepted_commitment_digest?: string;
}

interface JsonRpcResponse {
  jsonrpc: string;
  id: number | string | null;
  result?: {
    _meta?: { vap?: { verdict?: VapVerdictPayload; session_id?: string } };
    structuredContent?: Record<string, unknown>;
    [k: string]: unknown;
  };
  error?: { code: number; message: string };
}

export class VapClient {
  readonly baseUrl: string;
  sessionId: string | null = null;
  commitmentDigest: string | null = null;
  private readonly hmacSecret: string;
  private rpcId = 0;

  constructor(baseUrl: string, hmacSecret = "vap-dev-secret") {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.hmacSecret = hmacSecret;
  }

  /** HMAC-sign a canonical JSON payload. Returns a "hmac:<hex>" string. */
  sign(payload: Record<string, unknown>): string {
    const canonical = canonicalJson(payload);
    const value = createHmac("sha256", this.hmacSecret).update(canonical).digest("hex");
    return `hmac:${value}`;
  }

  private nextId(): number {
    this.rpcId += 1;
    return this.rpcId;
  }

  private async post(
    rpc: unknown
  ): Promise<{ body: JsonRpcResponse; sessionId: string | null }> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (this.sessionId) headers[SESSION_HEADER] = this.sessionId;
    const resp = await fetch(`${this.baseUrl}/mcp`, {
      method: "POST",
      headers,
      body: JSON.stringify(rpc),
    });
    const sid = resp.headers.get(SESSION_HEADER);
    const body = (await resp.json()) as JsonRpcResponse;
    return { body, sessionId: sid };
  }

  async openSession(
    commitment: ScopeCommitment,
    opts?: { sign?: boolean }
  ): Promise<VapResult> {
    const c: ScopeCommitment = {
      vap: VAP_VERSION,
      type: "scope_commitment",
      session_id: "pending",
      ...commitment,
    };
    if (opts?.sign && !c.signature) {
      const { signature: _omit, ...rest } = c;
      c.signature = this.sign(rest as unknown as Record<string, unknown>);
    }
    const rpc = {
      jsonrpc: "2.0",
      id: this.nextId(),
      method: "initialize",
      params: {
        protocolVersion: "2024-11-05",
        clientInfo: { name: "vap-ts-client", version: "0.1.0" },
        _meta: { vap: { scope_commitment: c } },
      },
    };
    const { body, sessionId } = await this.post(rpc);
    if (sessionId) this.sessionId = sessionId;
    const res = parseResult(body);
    if (res.acceptedCommitmentDigest) this.commitmentDigest = res.acceptedCommitmentDigest;
    return res;
  }

  async call(
    tool: string,
    args: Record<string, unknown>,
    intent: Intent
  ): Promise<VapResult> {
    const envelope = {
      vap: VAP_VERSION,
      type: "intent_call",
      session_id: this.sessionId,
      intent,
      call: { tool, arguments: args },
    };
    const rpc = {
      jsonrpc: "2.0",
      id: this.nextId(),
      method: "tools/call",
      params: { name: tool, arguments: args, _meta: { vap: { intent: envelope } } },
    };
    const { body } = await this.post(rpc);
    return parseResult(body);
  }

  async amend(opts: {
    addScope?: Partial<Scope>;
    increaseBudget?: {
      add_calls?: number;
      extend_deadline?: string;
      /** Map of named meters -> amount to ADD to each meter's ceiling in limits. */
      add_limits?: Record<string, number>;
    };
    reason?: string;
    newPlanDigest?: string;
    sign?: boolean;
  }): Promise<VapResult> {
    const payload: Record<string, unknown> = {
      vap: VAP_VERSION,
      type: "scope_amendment",
      session_id: this.sessionId,
      prev_commitment_digest: this.commitmentDigest ?? "sha256:0",
      reason: opts.reason ?? "re-planning",
    };
    if (opts.addScope !== undefined) payload.add_scope = opts.addScope;
    if (opts.increaseBudget !== undefined) payload.increase_budget = opts.increaseBudget;
    if (opts.newPlanDigest !== undefined) payload.new_plan_digest = opts.newPlanDigest;
    if (opts.sign !== false) payload.signature = this.sign(payload);
    const rpc = {
      jsonrpc: "2.0",
      id: this.nextId(),
      method: "vap/amend",
      params: { _meta: { vap: { amendment: payload } }, ...payload },
    };
    const { body } = await this.post(rpc);
    const res = parseResult(body);
    if (res.acceptedCommitmentDigest) this.commitmentDigest = res.acceptedCommitmentDigest;
    return res;
  }

  async getAudit(): Promise<unknown> {
    const url = this.sessionId
      ? `${this.baseUrl}/audit?session_id=${encodeURIComponent(this.sessionId)}`
      : `${this.baseUrl}/audit`;
    const resp = await fetch(url);
    return resp.json();
  }
}

function parseResult(body: JsonRpcResponse): VapResult {
  const result = body.result ?? {};
  const vap = result._meta?.vap?.verdict;
  return {
    verdict: vap?.verdict ?? (body.error ? "denied" : "unknown"),
    verification: vap?.verification ?? { checks: [], method: "static" },
    clarification: vap?.clarification,
    auditRef: vap?.audit_ref,
    acceptedCommitmentDigest: vap?.accepted_commitment_digest,
    result: result.structuredContent,
    raw: body,
  };
}

/** Stable key ordering for deterministic signatures. */
function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(obj[k])}`).join(",")}}`;
}
