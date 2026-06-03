# VAP Release Strategy

*A sequencing plan for taking the Verifiable Agent Protocol from draft to adoption — June 2026*

## The core principle

A protocol's value is its network effect; a spec with zero implementations is just a PDF. So **adoption leads, standardization follows.** Every phase below is gated on the previous one producing real usage, not on producing more documents. This mirrors how MCP and A2A actually won (vendor-led open spec + reference implementation under a foundation, *not* a standards-body-first path).

Equally important: **bet on the mechanisms, not the protocol framing.** There is a real chance MCP absorbs a native intent/purpose field, which would subsume "VAP the protocol." The transferable assets — the scope-commitment/amendment drift model, risk-rationed semantic verification, domain-blind side-effect metering — survive that outcome. Package and promote those as reusable ideas, so the effort pays off even if the wrapper gets commoditized.

## Phase 0 — Foundations (now, weeks 0–4)

Goal: a credible, self-contained artifact set that a stranger can run in five minutes.

- Publish the **reference implementation** (the existing harness) to a public repo under a permissive license (Apache-2.0 — patent grant matters for a protocol). Clean README, one-command `docker compose up`, the passing E2E suite as proof.
- Publish the **JSON Schemas** at a stable URL (the `$id`s already point at `vap.dev/schemas/0.1/`); make that resolvable.
- Ship the **Internet-Draft** (`draft-samal-vap-00`) to the IETF Datatracker as an individual submission. This is cheap, commits you to nothing, timestamps your authorship, and signals seriousness.
- Write a **600-word "why" post** (the cost/drift/audit problem + the three mechanisms). Lead with the problem and a runnable demo, not the spec.

Exit criterion: someone outside your circle stars the repo and runs it.

## Phase 1 — Land it as an MCP extension, not a rival protocol (months 1–3)

Goal: ride the incumbent's ecosystem instead of competing with it.

- MCP added a formal **extensions mechanism** in late 2025. Re-frame VAP as an **MCP extension** (it already rides in `_meta`, so this is natural). This is the single highest-leverage move: you inherit MCP's transport, auth, elicitation, and — crucially — its install base. "An MCP governance extension" is an easy yes; "a new agent protocol" is an uphill fight.
- Engage the **MCP community / SEP (spec enhancement) process** with the extension. Even if not merged, a serious proposal puts the idea in front of the people who decide MCP's roadmap (and, candidly, hedges the "MCP absorbs it" risk by making *you* the one proposing it).
- Build **one drop-in integration** with a popular MCP gateway or agent framework (LangChain, the MCP TypeScript/Python SDKs, an existing gateway like the open-source ones). A working `pip install` / `npm install` that adds VAP to an existing stack beats any amount of spec prose.

Exit criterion: VAP runs in front of a real, third-party MCP server with one config change.

## Phase 2 — Get a design partner (months 2–6)

Goal: one real deployment that produces a number you can quote.

- The buyer is **whoever pays when an agent runs away or acts incoherently**: fintech, infra/devops automation, customer-support automation, or any team running agents against money-moving or destructive tools. That's where "cost control + drift + audit" is a budget line, not a nicety.
- Land **one design partner** (even internal/friendly) and instrument it. The goal is a concrete result: "VAP caught N incoherent calls / capped runaway spend at $X / produced the audit trail that passed review." One real number is worth more than the whole spec.
- Use their feedback to cut the spec, not grow it. Resist scope creep — the tool-agnostic discipline you already enforced is the moat; protect it.

Exit criterion: a citable outcome from a non-toy deployment.

## Phase 3 — Standardize where it sticks (months 6–12+)

Goal: durability, only after traction.

- With adoption in hand, choose the standardization venue by where the energy is:
  - If MCP-centric: push the extension toward official MCP-ecosystem blessing.
  - If cross-protocol interest emerges (A2A/AGNTCY also want it): advance the Internet-Draft toward a working group, ideally aligning with the existing **IETF agent-identity efforts** (WIMSE, the agent-JWT drafts) rather than starting a turf war.
- **Do not target a full RFC as the goal.** It's slow, consensus-driven, and cedes design control. Treat an RFC as a *possible late outcome* if multi-vendor demand materializes — not the objective.
- Contribute the transferable mechanisms back as **standalone write-ups** (a short paper or well-cited blog posts on each of the three). This banks the intellectual contribution independently of whether "VAP" as a brand survives.

Exit criterion: at least one other implementer ships VAP support without your involvement.

## What to optimize at each gate

| Phase | The one thing that matters | The trap to avoid |
|---|---|---|
| 0 | Runnable demo + timestamped draft | Polishing the spec instead of shipping code |
| 1 | Framed as an MCP extension | Positioning as a competing protocol |
| 2 | One citable real-world result | Chasing many shallow integrations |
| 3 | Another independent implementer | Treating an RFC as the finish line |

## Positioning language (use consistently)

- **Yes:** "a thin, tool-agnostic governance extension for MCP that adds verifiable intent, drift control, cost budgets, and audit."
- **No:** "a new agent communication protocol to replace MCP." (Invites the protocol wars and the incumbent's veto.)

## The honest risk register

1. **MCP ships native intent.** Mitigation: be the one proposing it (Phase 1 SEP); keep the mechanisms portable.
2. **Semantic verification proves too costly/unreliable in practice.** Mitigation: the risk-rationed loop is designed for exactly this; measure it at the design partner and publish the economics either way.
3. **"It's just a gateway feature."** Mitigation: lead with the three mechanisms (drift-as-scope-escape, risk rationing, domain-blind metering), which are non-obvious and reusable, not with "we built a proxy."
4. **No adoption.** Mitigation: every phase is gated on real usage; if Phase 1 doesn't land an integration, stop and reconsider rather than over-investing in the spec.

## Minimal first week

1. Repo public, Apache-2.0, demo runs in one command.
2. Schemas resolvable at their `$id` URLs.
3. `draft-samal-vap-00` submitted to the IETF Datatracker.
4. The "why" post published, pointing at the demo.
5. One outreach message to the MCP community describing the extension.
