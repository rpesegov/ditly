"""Ошибки, которые можно показать человеку в форме."""


class UserError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def required_text(value: str, limit: int, field: str) -> str:
    cleaned = " ".join((value or "").split())
    if not cleaned:
        raise UserError(f"Заполните поле «{field}».")
    if len(cleaned) > limit:
        raise UserError(f"Поле «{field}» не длиннее {limit} символов.")
    return cleaned
