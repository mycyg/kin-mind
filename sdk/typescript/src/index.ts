import { operations, type Operation } from "./operations.js";
import type { operations as Contract } from "./schema.js";

export type { components, paths } from "./schema.js";
export type Body<K extends Operation> = K extends keyof Contract
  ? Contract[K] extends {
      requestBody: { content: { "application/json": infer B } };
    }
    ? B
    : unknown
  : unknown;

// Attachments, pages, clips and exports are bytes whatever their media type says: a JSON or
// NDJSON attachment is the file the owner stored, not a response to parse.
const binaryOperations = [
  "read_attachment",
  "read_page",
  "read_clip",
  "download_export",
] as const;
export type BinaryOperation = (typeof binaryOperations)[number];
const binary = new Set<Operation>(binaryOperations);

export type Output<K extends Operation> = K extends BinaryOperation
  ? ArrayBuffer
  : K extends
  | "receive_source"
  | "upload_source"
  | "read_source"
  | "create_memory"
  | "read_memory"
  | "revise_memory"
  | "recall"
  ? K extends keyof Contract
    ? Contract[K] extends {
        responses: { 200: { content: { "application/json": infer R } } };
      }
      ? R
      : any
    : any
  : any;

export type CallOptions<K extends Operation> = {
  body?: Body<K>;
  path?: Record<string, string>;
  query?: Record<string, string | number | boolean | undefined>;
  form?: FormData;
  signal?: AbortSignal;
};

export class Client {
  constructor(
    readonly url: string,
    readonly token: string,
    readonly fetcher: typeof fetch = globalThis.fetch.bind(globalThis),
  ) {}

  async call<K extends Operation>(
    operation: K,
    options: CallOptions<K> = {},
  ): Promise<Output<K>> {
    const response = await this.response(operation, options);
    if (binary.has(operation))
      return response.arrayBuffer() as Promise<Output<K>>;
    return response.headers.get("content-type")?.includes("json")
      ? response.json()
      : (response.arrayBuffer() as Promise<Output<K>>);
  }

  /** The checked response itself, for a caller that streams a large body instead of holding it. */
  async response<K extends Operation>(
    operation: K,
    options: CallOptions<K> = {},
  ): Promise<Response> {
    const spec = operations[operation];
    let path: string = spec.path;
    for (const [key, value] of Object.entries(options.path ?? {}))
      path = path.replace(`{${key}}`, encodeURIComponent(value));
    if (path.includes("{")) throw new Error("Missing path parameter");
    const url = new URL(this.url.replace(/\/$/, "") + path);
    for (const [key, value] of Object.entries(options.query ?? {}))
      if (value !== undefined) url.searchParams.set(key, String(value));
    const response = await this.fetcher(url, {
      method: spec.method,
      headers: {
        Authorization: `Bearer ${this.token}`,
        ...(options.form ? {} : { "Content-Type": "application/json" }),
      },
      body:
        options.form ??
        (options.body === undefined ? undefined : JSON.stringify(options.body)),
      signal: options.signal,
    });
    if (!response.ok)
      throw new Error(
        `MemoryPalace ${response.status}: ${(await response.text()).slice(0, 500)}`,
      );
    return response;
  }

  async upload(
    file: Blob,
    metadata: Record<string, unknown>,
    name = "attachment",
  ) {
    const form = new FormData();
    form.set("metadata", JSON.stringify(metadata));
    form.set("file", file, name);
    return this.call("upload_source", { form });
  }
}

export interface DeliveryStore {
  transaction<T>(
    callback: (tx: {
      has(id: string): Promise<boolean>;
      put(id: string, body: unknown): Promise<void>;
    }) => Promise<T>,
  ): Promise<T>;
}
export async function acceptDelivery(
  store: DeliveryStore,
  delivery: { id: string },
  effect: (tx: unknown, delivery: { id: string }) => Promise<void>,
) {
  return store.transaction(async (tx) => {
    if (await tx.has(delivery.id)) return false;
    await effect(tx, delivery);
    await tx.put(delivery.id, delivery);
    return true;
  });
}
