"""Checkbox dialogs for the disclaimer (``captation accept``, R-59).

The graphical dialog uses Tk from the standard library, in its own short-lived process (the
agent never opens it). The checkbox unlocks only after the text was scrolled to the end, and
*Accept* only works while the checkbox is ticked. Without a display the terminal variant asks
the user to tick the checkbox by typing ``x``; there is no flag that accepts on the user's behalf.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from captation.ui.i18n import Strings

_ITEM = re.compile(r"^(\d+\.|[-*])\s")


class NoDisplay(Exception):
    """No graphical session (or Tk is missing)."""


def _plain_lines(text: str) -> list[tuple[str, str]]:
    """(tag, paragraph) pairs for display: Markdown headings become bold lines, hard-wrapped
    lines are joined into paragraphs (list items stay separate), markers and emoji are dropped."""
    out: list[tuple[str, str]] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append(("plain", " ".join(para)))
            para.clear()

    def blank() -> None:
        flush()
        if out and out[-1] != ("plain", ""):
            out.append(("plain", ""))

    for raw in text.splitlines():
        line = raw.strip().removeprefix(">").strip()
        line = line.replace("**", "").replace("\u26a0", "").replace("\ufe0f", "").strip()
        if not line or line == "---":
            blank()
        elif line.startswith("#"):
            blank()
            out.append(("heading", line.lstrip("#").strip()))
        elif _ITEM.match(line):
            flush()
            para.append(line)
        else:
            para.append(line)
    flush()
    while out and out[-1] == ("plain", ""):
        out.pop()
    return out


def show_dialog(text: str, strings: Strings) -> bool:
    """Show the text with a checkbox. True only if the box was ticked and *Accept* pressed."""
    try:
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import ttk
    except ImportError as exc:
        raise NoDisplay("Tk is not available") from exc
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise NoDisplay(str(exc)) from exc

    result = {"accepted": False}
    root.title(strings("terms.title"))

    body = ttk.Frame(root, padding=12)
    body.pack(fill="both", expand=True)
    base = tkfont.nametofont("TkDefaultFont")
    wrap_px = base.measure("0") * 88
    intro = ttk.Label(body, text=strings("terms.intro"), wraplength=wrap_px, justify="left")

    box = ttk.Frame(body)
    view = tk.Text(box, wrap="word", width=88, height=20, padx=10, pady=8, font=base)
    bold = base.copy()
    bold.configure(weight="bold")
    view.tag_configure("heading", font=bold, spacing1=4, spacing3=2)
    view.tag_configure("plain", spacing3=2)
    for tag, line in _plain_lines(text):
        view.insert("end", line + "\n", (tag,))
    view.configure(state="disabled")

    hint = ttk.Label(body, text=strings("terms.scroll_hint"))
    checked = tk.BooleanVar(value=False)
    check = tk.Checkbutton(
        body,
        text=strings("terms.checkbox"),
        variable=checked,
        wraplength=wrap_px,
        justify="left",
        anchor="w",
        state="disabled",
    )
    buttons = ttk.Frame(body)
    accept_btn = ttk.Button(buttons, text=strings("terms.accept"), state="disabled")
    decline_btn = ttk.Button(buttons, text=strings("terms.decline"), command=root.destroy)

    def unlock_if_at_end(first: float | str, last: float | str) -> None:
        scrollbar.set(first, last)
        if float(last) >= 0.999 and str(check.cget("state")) == "disabled":
            check.configure(state="normal")
            hint.configure(text=strings("terms.tick_hint"))

    def on_toggle() -> None:
        accept_btn.configure(state="normal" if checked.get() else "disabled")

    def on_accept() -> None:
        if checked.get():
            result["accepted"] = True
            root.destroy()

    scrollbar = ttk.Scrollbar(box, command=view.yview)
    view.configure(yscrollcommand=unlock_if_at_end)
    check.configure(command=on_toggle)
    accept_btn.configure(command=on_accept)
    # Bottom-up packing: checkbox and buttons stay visible however small the window gets.
    intro.pack(side="top", anchor="w", pady=(0, 8))
    buttons.pack(side="bottom", fill="x")
    decline_btn.pack(side="right")
    accept_btn.pack(side="right", padx=(0, 8))
    check.pack(side="bottom", fill="x", pady=(4, 8))
    hint.pack(side="bottom", anchor="w", pady=(8, 0))
    box.pack(side="top", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    view.pack(side="left", fill="both", expand=True)
    root.update_idletasks()
    root.minsize(root.winfo_reqwidth() // 2, root.winfo_reqheight() // 2)

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return result["accepted"]


def terminal_checkbox(
    text: str,
    strings: Strings,
    *,
    show: Callable[[str], None],
    ask: Callable[[str], str],
) -> bool:
    """Terminal variant: print the text, then a checkbox the user ticks by typing ``x``."""
    show(text)
    show("")
    show(f"[ ] {strings('terms.checkbox')}")
    answer = ask(strings("terms.terminal_prompt"))
    return answer.strip().lower() == "x"
