// A small hash router: #/board, #/periods/12, ... No library needed for six screens.
import { useSyncExternalStore } from "react";

export type Route =
  | { name: "login" }
  | { name: "board" }
  | { name: "period"; periodId: number }
  | { name: "reports"; clientId: number }
  | { name: "followups"; clientId: number; periodId: number }
  | { name: "client"; periodId: number }
  | { name: "home" };

const PATTERNS: [RegExp, (m: RegExpMatchArray) => Route][] = [
  [/^#\/login$/, () => ({ name: "login" })],
  [/^#\/board$/, () => ({ name: "board" })],
  [/^#\/periods\/(\d+)$/, (m) => ({ name: "period", periodId: Number(m[1]) })],
  [/^#\/clients\/(\d+)\/reports$/, (m) => ({ name: "reports", clientId: Number(m[1]) })],
  [/^#\/clients\/(\d+)\/followups\/(\d+)$/, (m) =>
    ({ name: "followups", clientId: Number(m[1]), periodId: Number(m[2]) })],
  [/^#\/client\/(\d+)$/, (m) => ({ name: "client", periodId: Number(m[1]) })],
];

export function parse(hash: string): Route {
  for (const [re, make] of PATTERNS) {
    const m = hash.match(re);
    if (m) return make(m);
  }
  return { name: "home" };
}

function subscribe(cb: () => void) {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}

export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash);
  return parse(hash);
}

export function navigate(hash: string) {
  if (window.location.hash !== hash) window.location.hash = hash;
}
