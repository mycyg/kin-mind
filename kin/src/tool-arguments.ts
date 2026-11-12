/** Core publishes full JSON Schema; validate it without coercing arguments or changing open-object contracts. */
import { Ajv } from 'ajv'
import { ToolArgsError, type ToolDefinition } from '@deepseek-ai/dsh-tools'

const validator = new Ajv({ allErrors: true, strict: true, coerceTypes: false, useDefaults: false, removeAdditional: false })

export function checkedToolCall<T>(schema: Record<string, unknown>, args: unknown, invoke: () => Promise<T>): Promise<T> {
  const validate = validator.compile(schema)
  if (!validate(args)) throw new ToolArgsError((validate.errors ?? []).map(error => `${error.instancePath || '/'} ${error.message ?? 'is invalid'}`))
  return invoke()
}

/** Validate before the MCP bridge can normalize a non-object argument to an empty object. */
export function checkedTool(definition: ToolDefinition): ToolDefinition {
  return { ...definition, execute: (args, execution) => checkedToolCall(definition.parameters, args, () => definition.execute(args, execution)) }
}
