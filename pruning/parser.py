"""Small recursive-descent parser for pruning expressions.

Supported examples::

    x > 10 AND y < 3
    x IS NULL OR y IS NOT NULL
    NOT (a = 1 OR b != 2)
    a + 1 < b
"""

from __future__ import annotations

from typing import Any

from .expressions import (
    And,
    Arithmetic,
    Column,
    Compare,
    Expr,
    IsNull,
    Literal,
    Not,
    Or,
)


class ExpressionParseError(ValueError):
    """Raised when an expression string cannot be parsed."""


_TOKEN_WORDS = {
    "AND",
    "OR",
    "NOT",
    "IS",
    "NULL",
    "TRUE",
    "FALSE",
    "ISNULL",
}


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(("LPAREN", ch))
            i += 1
            continue
        if ch == ")":
            tokens.append(("RPAREN", ch))
            i += 1
            continue
        if ch == ",":
            tokens.append(("COMMA", ch))
            i += 1
            continue
        if ch in "+-*/":
            tokens.append(("OP", ch))
            i += 1
            continue
        if text.startswith("<=", i) or text.startswith(">=", i) or text.startswith("<>", i) or text.startswith("!=", i) or text.startswith("==", i):
            tokens.append(("CMP", text[i : i + 2]))
            i += 2
            continue
        if ch in "=<>":
            tokens.append(("CMP", ch))
            i += 1
            continue
        if ch.isdigit():
            j = i
            while j < n and text[j].isdigit():
                j += 1
            tokens.append(("NUM", text[i:j]))
            i = j
            continue
        if ch == '"' or ch == "`":
            quote = ch
            j = i + 1
            chars: list[str] = []
            while j < n and text[j] != quote:
                if text[j] == "\\" and j + 1 < n:
                    chars.append(text[j + 1])
                    j += 2
                else:
                    chars.append(text[j])
                    j += 1
            if j >= n:
                raise ExpressionParseError("unterminated quoted identifier")
            name = "".join(chars)
            tokens.append(("ID", name))
            i = j + 1
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("ID", text[i:j]))
            i = j
            continue
        raise ExpressionParseError(f"unexpected character {ch!r} at position {i}")
    tokens.append(("EOF", ""))
    return tokens


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokenize(text)
        self.pos = 0

    @property
    def kind(self) -> str:
        return self.tokens[self.pos][0]

    @property
    def value(self) -> str:
        return self.tokens[self.pos][1]

    def advance(self) -> tuple[str, str]:
        token = self.tokens[self.pos]
        if self.kind != "EOF":
            self.pos += 1
        return token

    def expect(self, kind: str, value: str | None = None) -> tuple[str, str]:
        token_kind, token_value = self.tokens[self.pos]
        if token_kind != kind or (value is not None and token_value.upper() != value.upper()):
            wanted = f"{kind}:{value}" if value else kind
            raise ExpressionParseError(
                f"expected {wanted}, got {token_kind}:{token_value!r} in {self.text!r}"
            )
        return self.advance()

    def parse(self) -> Expr:
        expr = self.parse_or()
        if self.kind != "EOF":
            raise ExpressionParseError(f"unexpected trailing token {self.value!r} in {self.text!r}")
        return expr

    def _is_keyword(self, word: str) -> bool:
        return self.kind == "ID" and self.value.upper() == word

    def _eat_keyword(self, word: str) -> bool:
        if self._is_keyword(word):
            self.advance()
            return True
        return False

    def parse_or(self) -> Expr:
        node = self.parse_and()
        while self._eat_keyword("OR"):
            rhs = self.parse_and()
            node = Or(node, rhs)
        return node

    def parse_and(self) -> Expr:
        node = self.parse_not()
        while self._eat_keyword("AND"):
            rhs = self.parse_not()
            node = And(node, rhs)
        return node

    def parse_not(self) -> Expr:
        if self._eat_keyword("NOT"):
            return Not(self.parse_not())
        return self.parse_is_null()

    def parse_is_null(self) -> Expr:
        node = self.parse_comparison()
        if self._eat_keyword("IS"):
            negated = self._eat_keyword("NOT")
            self.expect("ID", "NULL")
            return IsNull(node, negated)
        return node

    def parse_comparison(self) -> Expr:
        left = self.parse_additive()
        if self.kind == "CMP":
            op = self.advance()[1]
            right = self.parse_additive()
            return Compare(left, op, right)
        return left

    def parse_additive(self) -> Expr:
        node = self.parse_unary()
        while self.kind == "OP" and self.value in ("+", "-"):
            op = self.advance()[1]
            rhs = self.parse_unary()
            node = Arithmetic(node, op, rhs)
        return node

    def parse_unary(self) -> Expr:
        if self.kind == "OP" and self.value == "-":
            self.advance()
            operand = self.parse_unary()
            if isinstance(operand, Literal) and isinstance(operand.value, int) and not isinstance(operand.value, bool):
                return Literal(-operand.value)
            return Arithmetic(Literal(0), "-", operand)
        if self.kind == "OP" and self.value == "+":
            self.advance()
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self) -> Expr:
        if self.kind == "LPAREN":
            self.advance()
            node = self.parse_or()
            self.expect("RPAREN")
            return node

        if self.kind == "NUM":
            raw = self.advance()[1]
            return Literal(int(raw))

        if self.kind == "ID":
            word = self.value.upper()
            if word == "TRUE":
                self.advance()
                return Literal(True)
            if word == "FALSE":
                self.advance()
                return Literal(False)
            if word == "NULL":
                self.advance()
                return Literal(None)
            if word == "ISNULL":
                self.advance()
                self.expect("LPAREN")
                arg = self.parse_or()
                self.expect("RPAREN")
                return IsNull(arg, False)

            name = self.advance()[1]
            # Function-style null checks are also accepted.
            if self.kind == "LPAREN" and name.upper() in ("ISNULL", "ISNOTNULL"):
                self.advance()
                arg = self.parse_or()
                self.expect("RPAREN")
                return IsNull(arg, name.upper() == "ISNOTNULL")
            return Column(name)

        raise ExpressionParseError(f"expected expression, got {self.kind}:{self.value!r}")


def parse_expr(text: str) -> Expr:
    """Parse a SQL-like pruning filter expression."""
    if not isinstance(text, str) or not text.strip():
        raise ExpressionParseError("expression must be a non-empty string")
    return _Parser(text).parse()
