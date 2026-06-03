// Minimal ambient declarations used by vapClient.ts.
//
// In a normal install these come from `@types/node` (declared in package.json) and
// the DOM lib (fetch). This shim lets `tsc --noEmit` succeed in air-gapped CI where
// the npm registry is unreachable. Delete it once @types/node is installed.

declare module "node:crypto" {
  export interface Hmac {
    update(data: string): Hmac;
    digest(encoding: "hex"): string;
  }
  export function createHmac(algorithm: string, key: string): Hmac;
}
