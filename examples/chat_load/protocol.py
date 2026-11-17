from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ROOM_PATTERN = "^[a-z0-9-]{1,40}$"

type RoomId = Annotated[str, Field(pattern=ROOM_PATTERN)]
type ClientId = Annotated[str, Field(pattern=ROOM_PATTERN)]


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Send(Model):
    id: int = Field(ge=0)
    text: str = Field(max_length=500)


class Ack(Model):
    type: Literal["ack"] = "ack"
    id: int
    count: int


class Message(Model):
    type: Literal["message"] = "message"
    sender: str
    id: int
    text: str
    count: int


class JobRequest(Model):
    room: RoomId
    size: int = Field(ge=1, le=1_000_000)


class JobAccepted(Model):
    job: str


class JobDone(Model):
    type: Literal["job"] = "job"
    job: str
    result: int | None


type Push = Annotated[Ack | Message | JobDone, Field(discriminator="type")]


class RoomStats(Model):
    messages: int
    jobs: int
