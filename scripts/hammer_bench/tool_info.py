"""BenchExec tool-info module for the Vampire hammer on mathlib theorems.

Use it as `tool="hammer_bench.tool_info"` with this package's parent directory
on PYTHONPATH (see ../README.md). Every task carries the property
`proves.prp` with expected verdict `true`, so BenchExec counts each independent
kernel-checked proof as a correct result. Hammer failures are not wrong
results: a theorem the hammer cannot prove is reported as `unknown` (search
or translation gave up) or as `ERROR (<kind>)`.
"""

import json

import benchexec.result as result
import benchexec.tools.template

RESULT_PREFIX = "HAMMER-RESULT: "

# Expected limitations of the hammer, rather than malfunctions.
UNKNOWN_KINDS = {"search_timeout", "search_unsuccessful", "unsupported_input", "proof_timeout", "excluded"}


def hammer_result(output):
    for line in reversed(output):
        if line.startswith(RESULT_PREFIX):
            try:
                row = json.loads(line[len(RESULT_PREFIX):])
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                return row
    return None


class Tool(benchexec.tools.template.BaseTool2):
    def name(self):
        return "Vampire hammer"

    def executable(self, tool_locator):
        return tool_locator.find_executable("run-hammer-task", subdir="scripts")

    def version(self, executable):
        return self._version_from_tool(executable)

    def cmdline(self, executable, options, task, rlimits):
        return [executable, *options, task.single_input_file]

    def determine_result(self, run):
        row = hammer_result(run.output)
        if row is None:
            return result.RESULT_ERROR
        if row["status"] == "proved":
            return result.RESULT_TRUE_PROP
        kind = row.get("failure_kind", row["status"])
        if kind in UNKNOWN_KINDS:
            return result.RESULT_UNKNOWN
        # Includes reuses_target and sorry: never counted as proofs.
        return f"{result.RESULT_ERROR} ({kind})"

    def get_value_from_output(self, output, identifier):
        row = hammer_result(output)
        if row is None:
            return None
        if identifier == "outcome":
            return row.get("failure_kind", row["status"])
        if identifier == "route":
            return row.get("route")
        if identifier == "hammer-seconds":
            return row.get("seconds")
        if identifier == "vampire-seconds":
            return row.get("search_seconds")
        return row.get(identifier)
