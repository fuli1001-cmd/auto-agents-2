"""Validated user input; choices and editable prose have distinct contracts."""
from __future__ import annotations

import re
from contextlib import nullcontext

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.validation import ValidationError, Validator

from .errors import AA2Error


def user_text(text: str) -> str:
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127)).strip()


def question_text(text: str) -> str:
    # Strip a legacy requirement prefix, never identifiers inside URLs, branch
    # names, or user-selected model/profile names.
    return re.sub(r"(?m)^R\d+(?:[^\S\r\n]+|[：:][^\S\r\n]*)", "", user_text(text))


def is_prototype_approval(question) -> bool:
    if not isinstance(question, str):
        return question.purpose == "prototype_approval"
    # Compatibility with proposals saved before questions had an explicit purpose.
    return bool(re.search(r"原型|prototype", question, re.I) and
                re.search(r"批准|审批|审阅|审核|通过|approv|review", question, re.I))


def choice_spec(labels: list[str], values: list[str] | None = None, *, yes_no=False) -> dict:
    keys = ["y", "n"] if yes_no else [str(i + 1) for i in range(len(labels))]
    if yes_no and len(labels) != 2:
        raise ValueError("y/n requires two options")
    return {"kind": "choice", "options": [
        {"key": k, "label": user_text(label), "value": value}
        for k, label, value in zip(keys, labels, values if values is not None else labels, strict=True)
    ]}


def validate_answer(answer: str, interaction: dict) -> str:
    if interaction.get("kind") == "choice":
        value = answer.strip()
        keys = [o["key"] for o in interaction["options"]]
        if value not in keys:
            raise AA2Error("请输入 " + "/".join(keys) + " 中的一个选项。")
        return value
    if not answer.strip():
        raise AA2Error("内容不能为空，请继续输入。")
    return answer


class AnswerValidator(Validator):
    def __init__(self, interaction):
        self.interaction = interaction

    def validate(self, document):
        try:
            validate_answer(document.text, self.interaction)
        except AA2Error as exc:
            raise ValidationError(message=str(exc), cursor_position=len(document.text)) from exc


def wrap_prompt(text, columns):
    """PromptSession's multiline prefix does not wrap long lines by itself."""
    lines = []
    for line in text.expandtabs(4).split("\n"):
        row, width = [], 0
        for char in line:
            size = get_cwidth(char)
            if row and width + size > columns:
                lines.append("".join(row))
                row, width = [], 0
            row.append(char)
            width += size
        lines.append("".join(row))
    return "\n".join(lines)


async def prompt_answer(session, question: dict) -> str:
    interaction = question.get("interaction", {})
    choice = interaction.get("kind") == "choice"
    message = "\n" + user_text(question["prompt"]) + "\n"
    bindings = KeyBindings()
    if choice:
        for option in interaction["options"]:
            message += f"{option['key']}. {option['label']}\n"
        message += "请选择 [" + "/".join(o["key"] for o in interaction["options"]) + "]："
        # Ctrl+D is not EOF or submission for a choice.
        @bindings.add("c-d")
        def ignore(event):
            pass
    else:
        message += "回车换行，Ctrl+D 提交；方向键移动，Backspace/Delete 删除。\n> "

        @bindings.add("c-d")
        def submit(event):
            event.current_buffer.validate_and_handle()

    # A dumb-terminal prompt is printed before prompt_toolkit enters raw mode.
    # Enter it first so an immediate Ctrl+C cannot be swallowed by line discipline.
    output = getattr(session, "output", None)
    if output is not None:
        message = wrap_prompt(message, max(2, output.get_size().columns - 1))
    prompt_input = getattr(session, "input", None)
    with prompt_input.raw_mode() if prompt_input is not None else nullcontext():
        if output is not None:
            # Long static prefixes are clipped vertically by PromptSession's
            # layout. Keep the full question in terminal scrollback instead.
            before, _, message = message.rpartition("\n")
            print_formatted_text(before, output=output)
        return await session.prompt_async(
            message, multiline=not choice, key_bindings=bindings,
            validator=AnswerValidator(interaction), validate_while_typing=False,
            prompt_continuation="  ", enable_history_search=False,
        )
