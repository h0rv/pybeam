import code
import codeop
from typing import override

from pybeam.runtime.attach import Connection


class Console(code.InteractiveConsole):
    def __init__(self, connection: Connection) -> None:
        super().__init__()
        self.connection = connection

    @override
    def runsource(self, source: str, filename: str = "<input>", symbol: str = "single") -> bool:
        try:
            if codeop.compile_command(source, filename, symbol) is None:
                return True
        except SyntaxError, OverflowError, ValueError:
            self.showsyntaxerror(filename)
            return False
        result = self.connection.request("evaluate", source)
        if result:
            self.write(result if result.endswith("\n") else result + "\n")
        return False
