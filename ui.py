"""Rich presentation, kept separate from lookups and JSON serialization."""
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def console() -> Console:
    # Resolve stdout on each call so redirection and capture keep working.
    return Console(markup=False, highlight=False)


def value_view(value: Any) -> Any:
    if value is None or value == "" or value == [] or value == {}:
        return Text("no data", style="dim")
    if isinstance(value, bool):
        return Text("yes" if value else "no", style="green" if value else "yellow")
    if isinstance(value, dict):
        return fields_table(value)
    if isinstance(value, (list, tuple)):
        return Group(*(value_view(item) for item in value))
    # Text prevents external values from being interpreted as Rich markup.
    return Text(str(value))


def fields_table(data: dict[str, Any]) -> Table:
    table = Table(box=box.SIMPLE, show_header=False, expand=True, padding=(0, 1))
    table.add_column("Field", style="cyan", ratio=1, overflow="fold")
    table.add_column("Value", ratio=3, overflow="fold")
    for key, value in data.items():
        table.add_row(Text(str(key).replace("_", " ")), value_view(value))
    return table


def show_result(data: dict[str, Any], target: Console | None = None) -> None:
    (target or console()).print(Panel(fields_table(data), title="Results", border_style="cyan"))


def show_menu(app: str, version: str, entries: dict, target: Console | None = None) -> None:
    terminal = target or console()
    terminal.print(Panel(
        Text.assemble((app, "bold cyan"), (f"  v{version}\n", "dim"),
                      ("Public information. Clear results.", "white")),
        border_style="cyan", padding=(1, 2)))
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    table.add_column("#", style="bold cyan", width=3)
    table.add_column("Tool", overflow="fold")
    table.add_column("Mode", style="dim", width=8)
    for number, (title, _) in entries.items():
        mode = "Offline" if number in {"6", "9", "11"} else "Network"
        if number == "0":
            mode = ""
        table.add_row(number, Text(title), mode)
    terminal.print(table)
    terminal.print(Text("Choose a number, then follow the prompts. Ctrl+C cancels an operation.", style="dim"))


def show_error(message: str) -> None:
    console().print(Panel(Text(message), title="Error", border_style="red"))
