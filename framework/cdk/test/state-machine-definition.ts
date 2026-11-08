import { Template } from "aws-cdk-lib/assertions";

/** The members of a state that name other states. */
interface StateTransitions {
  readonly Next?: string;
  readonly Default?: string;
  readonly Choices?: readonly { readonly Next?: string }[];
  readonly Catch?: readonly { readonly Next: string }[];
  readonly ItemProcessor?: StateGraph;
}

/** A definition, or a Map's processor, as its transitions. */
interface StateGraph {
  readonly StartAt: string;
  readonly States: Readonly<Record<string, StateTransitions>>;
}

/** The states a state can go to next: `Next`, `Default`, each Choice, and each Catch. */
const stateTransitions = (state: StateTransitions): string[] => [
  ...(state.Next === undefined ? [] : [state.Next]),
  ...(state.Default === undefined ? [] : [state.Default]),
  ...(state.Choices ?? []).flatMap((choice) => (choice.Next === undefined ? [] : [choice.Next])),
  ...(state.Catch ?? []).map((caught) => caught.Next),
];

const reachableStates = ({
  graph,
  from,
}: {
  readonly graph: StateGraph;
  readonly from: string;
}): Set<string> => {
  const seen = new Set<string>([from]);
  const pending = [from];
  for (let current = pending.pop(); current !== undefined; current = pending.pop()) {
    if (!Object.hasOwn(graph.States, current)) continue;
    for (const target of stateTransitions(graph.States[current]))
      if (!seen.has(target)) {
        seen.add(target);
        pending.push(target);
      }
  }
  return seen;
};

const allStateNames = (graph: StateGraph): string[] =>
  Object.entries(graph.States).flatMap(([name, state]) => [
    name,
    ...(state.ItemProcessor === undefined ? [] : allStateNames(state.ItemProcessor)),
  ]);

const scopeProblems = (graph: StateGraph): string[] => {
  const problems: string[] = [];
  for (const [name, state] of Object.entries(graph.States))
    for (const target of stateTransitions(state))
      if (!(target in graph.States)) problems.push(`${name} goes to missing ${target}`);
  const reached = reachableStates({ graph, from: graph.StartAt });
  for (const name of Object.keys(graph.States))
    if (!reached.has(name)) problems.push(`${name} is not reachable`);
  for (const state of Object.values(graph.States))
    if (state.ItemProcessor !== undefined) problems.push(...scopeProblems(state.ItemProcessor));
  return problems;
};

/** A state name used twice across Map processors, a missing transition target, and an unreachable state. */
export const structuralProblems = (graph: StateGraph): string[] => {
  const names = allStateNames(graph);
  const duplicates = [...new Set(names.filter((name, index) => names.indexOf(name) !== index))];
  return [...duplicates.map((name) => `${name} is used more than once`), ...scopeProblems(graph)];
};

export interface AslTestState {
  readonly Type: string;
  readonly QueryLanguage?: string;
  readonly Resource?: string;
  readonly Parameters?: Record<string, unknown>;
  /** A JSONata state's call arguments. */
  readonly Arguments?: Record<string, unknown>;
  /** A JSONata state's variable assignments. */
  readonly Assign?: Record<string, unknown>;
  /** A JSONata state's output expression. */
  readonly Output?: string;
  readonly TimeoutSeconds?: number;
  readonly Error?: string;
  readonly Cause?: string;
  readonly Credentials?: { readonly "RoleArn.$"?: string; readonly RoleArn?: string };
  readonly Retry?: readonly {
    readonly ErrorEquals: readonly string[];
    readonly IntervalSeconds: number;
    readonly MaxAttempts: number;
    readonly BackoffRate: number;
    readonly JitterStrategy: string;
  }[];
  readonly Catch?: readonly {
    readonly ErrorEquals: readonly string[];
    readonly Next: string;
    readonly ResultPath?: string | null;
    readonly Output?: string;
    readonly Assign?: Record<string, unknown>;
  }[];
  readonly Choices?: readonly ({ readonly Next: string } & Record<string, unknown>)[];
  readonly Default?: string;
  readonly Next?: string;
  readonly End?: boolean;
  readonly ResultSelector?: Record<string, unknown>;
  readonly ResultPath?: string | null;
  readonly ErrorPath?: string;
  readonly CausePath?: string;
  readonly ItemsPath?: string;
}

export interface AslTestDefinition {
  readonly StartAt: string;
  readonly States: Record<string, AslTestState>;
}

/**
 * The definition of the named state machine, with each CloudFormation reference replaced by
 * `${Ref}` or `${LogicalId.Attribute}` so the document parses as JSON.
 */
export const renderedDefinition = (
  template: Template,
  stateMachineName: string,
): AslTestDefinition => {
  const machines = Object.values(
    template.findResources("AWS::StepFunctions::StateMachine", {
      Properties: { StateMachineName: stateMachineName },
    }),
  );
  expect(machines).toHaveLength(1);
  const definition: unknown = machines[0].Properties.DefinitionString;
  return JSON.parse(flatten(definition)) as AslTestDefinition;
};

const flatten = (value: unknown): string => {
  if (typeof value === "string") return value;
  const join = (value as { "Fn::Join"?: [string, unknown[]] })["Fn::Join"];
  if (join !== undefined) return join[1].map(flatten).join(join[0]);
  const ref = (value as { Ref?: string }).Ref;
  if (ref !== undefined) return `\${${ref}}`;
  const getAtt = (value as { "Fn::GetAtt"?: [string, string] })["Fn::GetAtt"];
  if (getAtt !== undefined) return `\${${getAtt[0]}.${getAtt[1]}}`;
  throw new Error(`Unexpected definition part ${JSON.stringify(value)}`);
};

/** Every Task state, by name. */
export const taskStates = (definition: AslTestDefinition): [string, AslTestState][] =>
  Object.entries(definition.States).filter(([, state]) => state.Type === "Task");

/** Every state reachable from `from`, including it; a transition to a missing state is skipped. */
export const reachableFrom = (definition: AslTestDefinition, from: string): Set<string> =>
  reachableStates({ graph: { StartAt: definition.StartAt, States: definition.States }, from });

/**
 * Evaluates a `States.Format('<template>', $.a, $.b)` call against values by JSONPath, the way
 * Step Functions does: `\` escapes the next character and each unescaped `{}` takes the next value.
 */
export const evaluateFormat = (
  expression: string,
  values: Readonly<Record<string, string>>,
): string => {
  const match = /^States\.Format\('((?:\\.|[^'\\])*)'((?:, \$[.\w]+)*)\)$/s.exec(expression);
  if (match === null) throw new Error(`Not a States.Format call: ${expression}`);
  const args = match[2] === "" ? [] : match[2].slice(2).split(", ");
  let result = "";
  let argument = 0;
  const template = match[1];
  for (let index = 0; index < template.length; index++) {
    const character = template[index];
    if (character === "\\") {
      result += template[++index];
    } else if (character === "{" && template[index + 1] === "}") {
      const path = args[argument++];
      if (!(path in values)) throw new Error(`No test value for ${path}`);
      result += values[path];
      index++;
    } else {
      result += character;
    }
  }
  if (argument !== args.length)
    throw new Error("States.Format got more arguments than placeholders");
  return result;
};

/** A policy statement as the template holds it, with `Action` and `Resource` as arrays. */
export interface TestStatement {
  readonly Sid?: string;
  readonly Effect: string;
  readonly Action: unknown[];
  readonly Resource: unknown[];
  readonly Condition?: unknown;
}

/** The identity policy statements attached to the role with the given name. */
export const roleStatements = (template: Template, roleName: string): TestStatement[] => {
  const roles = template.findResources("AWS::IAM::Role", { Properties: { RoleName: roleName } });
  const [roleId] = Object.keys(roles);
  expect(roleId).toBeDefined();
  const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : [value]);
  return Object.values(template.findResources("AWS::IAM::Policy"))
    .filter((policy) =>
      (policy.Properties.Roles as { Ref: string }[]).some((role) => role.Ref === roleId),
    )
    .flatMap((policy) => policy.Properties.PolicyDocument.Statement as Record<string, unknown>[])
    .map((statement) => ({
      ...(statement.Sid === undefined ? {} : { Sid: statement.Sid as string }),
      Effect: statement.Effect as string,
      Action: asArray(statement.Action),
      Resource: asArray(statement.Resource),
      ...(statement.Condition === undefined ? {} : { Condition: statement.Condition }),
    }));
};

/** Statements CDK adds for state machine logging and X-Ray tracing. */
export const isCdkLoggingOrTracing = (statement: TestStatement): boolean =>
  statement.Action.every(
    (action) => String(action).startsWith("logs:") || String(action).startsWith("xray:"),
  );

/** `arn:<partition>` followed by the rest, as CDK renders an ARN built with `Aws.PARTITION`. */
export const partitionJoinArn = (rest: string): unknown => ({
  "Fn::Join": ["", ["arn:", { Ref: "AWS::Partition" }, rest]],
});
