from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolReturnPart, UserPromptPart

from examples.agents.agent import agent, plan, scenario
from examples.agents.protocol import ToolName
from examples.agents.tools import executions, isolated


@pytest.mark.parametrize("prompt", ["", "normal", "Find something"])
def test_empty_and_normal_prompts_start_with_lookup(prompt: str) -> None:
    assert scenario(prompt) == "normal"
    assert plan([ModelRequest(parts=[UserPromptPart(prompt)])])[1:] == ("lookup", "step-1")


@pytest.mark.parametrize("mode", ["hang", "burn", "crash"])
def test_scenarios_always_start_with_lookup(mode: str) -> None:
    messages = [ModelRequest(parts=[UserPromptPart(mode)])]
    assert plan(messages)[1:] == ("lookup", "step-1")
    messages.append(ModelRequest(parts=[ToolReturnPart("lookup", "saved", "step-1")]))
    assert plan(messages)[1:] == (mode, "step-2")
    assert plan(messages)[1:] == (mode, "step-2")


class RecordingTools:
    def __init__(self) -> None:
        self.calls: list[tuple[ToolName, str]] = []

    async def execute(self, name: ToolName, call_id: str) -> str:
        self.calls.append((name, call_id))
        return "saved " + call_id


@pytest.mark.asyncio
async def test_nonstream_and_history_have_identical_call_ids() -> None:
    tools = RecordingTools()
    result = await agent.run("normal", deps=tools)
    assert tools.calls == [("lookup", "step-1"), ("lookup", "step-2"), ("lookup", "step-3")]
    assert result.output.startswith("Finished.")
    messages = result.all_messages()
    assert isinstance(messages[-1], ModelResponse)
    tools.calls.clear()
    resumed = await agent.run(message_history=messages[:-1], deps=tools)
    assert resumed.output == result.output
    assert tools.calls == []


@pytest.mark.asyncio
async def test_invalid_isolated_payload_and_empty_execution_counter(tmp_path: Path) -> None:
    assert executions(tmp_path, "missing") == {}
    with pytest.raises(ValidationError):
        await isolated(b'"not a tool request"')
