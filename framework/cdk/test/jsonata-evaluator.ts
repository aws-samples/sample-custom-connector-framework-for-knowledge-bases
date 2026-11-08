import { createHash, randomUUID } from "node:crypto";
import jsonata from "jsonata";

/** One Step Functions JSONata expression, with or without its `{% %}`, and what it reads. */
export interface EvaluateRequest {
  readonly expression: string;
  readonly input?: unknown;
  /** Variables by name without `$`, such as `{ error: {...}, states: { input: {...} } }`. */
  readonly bindings?: Readonly<Record<string, unknown>>;
}

/** Evaluates the expression offline, with the Step Functions functions `$uuid`, `$parse`, and `$hash` registered. */
export const evaluate = async (request: EvaluateRequest): Promise<unknown> => {
  const body = request.expression.trim().replace(/^\{%/, "").replace(/%\}$/, "");
  const expression = jsonata(body);
  expression.registerFunction("uuid", () => randomUUID());
  expression.registerFunction("parse", (text: string): unknown => JSON.parse(text));
  expression.registerFunction("hash", (text: string, algorithm: string) =>
    createHash(algorithm.replace("-", "").toLowerCase()).update(text).digest("hex"),
  );
  return expression.evaluate(request.input, { ...request.bindings });
};
