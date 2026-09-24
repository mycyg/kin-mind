import { Client } from "../../sdk/typescript/src/index";
export const api = new Client(location.origin, "");
export function setToken(token: string) {
  Object.assign(api, { token });
  sessionStorage.setItem("memorypalace-token", token);
}
export const initialToken =
  new URLSearchParams(location.hash.slice(1)).get("token") ??
  sessionStorage.getItem("memorypalace-token") ??
  "";
if (initialToken) setToken(initialToken);
if (location.hash) history.replaceState(null, "", location.pathname);
export const scopeDefault = {
  project: "personal",
  persona: "default",
  collection: "default",
  world: "real",
};
export type Scope = typeof scopeDefault;
const scopeKeys = ["project", "persona", "collection", "world"] as const;
// The console opens on the scope it was last pointed at, so a deployment that lives in
// another persona is not greeted with an empty default one each time.
export function savedScope(): Scope {
  try {
    const value = JSON.parse(localStorage.getItem("memorypalace-scope") ?? "null");
    if (scopeKeys.every((key) => typeof value?.[key] === "string"))
      return Object.fromEntries(scopeKeys.map((key) => [key, value[key]])) as Scope;
  } catch {}
  return scopeDefault;
}
// Whether this browser had chosen a scope before this page loaded. Until it has, the console
// opens on the scope the service reports as its own (health `default_scope`, E3-03).
export const scopeWasSaved = (() => {
  try {
    return localStorage.getItem("memorypalace-scope") !== null;
  } catch {
    return false;
  }
})();
export function saveScope(scope: Scope) {
  try {
    localStorage.setItem("memorypalace-scope", JSON.stringify(scope));
  } catch {}
}
// A downloaded body goes to a Blob, which the browser may keep on disk, and never to an
// ArrayBuffer held in page memory: a full backup can be larger than the page may hold.
export async function save(response: Response, name: string, type?: string) {
  const body = await response.blob();
  const url = URL.createObjectURL(type ? new Blob([body], { type }) : body);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
export const commandId = () => crypto.randomUUID();
export const stamp = (value?: string) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
