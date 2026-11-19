from pathlib import Path

from fastapi.templating import Jinja2Templates

from examples.agents.protocol import Snapshot

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def transcript(snapshot: Snapshot) -> str:
    return templates.get_template("transcript.html").render(snapshot=snapshot)
