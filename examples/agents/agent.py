import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from examples.agents.protocol import Scenario, ToolName


class ToolRunner(Protocol):
    async def execute(self, name: ToolName, call_id: str) -> str: ...


def scenario(prompt: str) -> Scenario:
    for name in ("hang", "burn", "crash"):
        if name in prompt.lower():
            return name
    return "normal"


def plan(messages: Sequence[ModelMessage]) -> tuple[str, ToolName | None, str]:
    prompt = " ".join(
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )
    mode = scenario(prompt)
    names: list[ToolName] = ["lookup", "lookup", "lookup"] if mode == "normal" else ["lookup", mode]
    returned = {
        part.tool_call_id
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    for index, name in enumerate(names):
        call_id = "step-" + str(index + 1)
        if call_id not in returned:
            return (
                "I am checking the next step before calling " + name + ". ",
                name,
                call_id,
            )
    return "Finished. Tool results are recorded in the ledger below.", None, ""


async def function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    text, name, call_id = plan(messages)
    if name is None:
        return ModelResponse(parts=[TextPart(text)])
    return ModelResponse(parts=[TextPart(text), ToolCallPart(name, {}, tool_call_id=call_id)])


async def stream_function(
    messages: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
    text, name, call_id = plan(messages)
    for word in text.split(" "):
        await asyncio.sleep(0.05)
        yield word + " "
    if name is not None:
        yield {0: DeltaToolCall(name=name, json_args="{}", tool_call_id=call_id)}


agent = Agent[ToolRunner, str](FunctionModel(function, stream_function=stream_function))


async def call(ctx: RunContext[ToolRunner], name: ToolName) -> str:
    if ctx.tool_call_id is None:
        raise ValueError("A tool call must have a deterministic ID")
    return await ctx.deps.execute(name, ctx.tool_call_id)


@agent.tool
async def lookup(ctx: RunContext[ToolRunner]) -> str:
    return await call(ctx, "lookup")


@agent.tool
async def hang(ctx: RunContext[ToolRunner]) -> str:
    return await call(ctx, "hang")


@agent.tool
async def burn(ctx: RunContext[ToolRunner]) -> str:
    return await call(ctx, "burn")


@agent.tool
async def crash(ctx: RunContext[ToolRunner]) -> str:
    return await call(ctx, "crash")
