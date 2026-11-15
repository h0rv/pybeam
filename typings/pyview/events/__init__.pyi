from dataclasses import dataclass

@dataclass
class InfoEvent:
    name: str
    payload: object = ...
