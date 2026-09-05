from ..domain import CompletionContract


class LegacyCompiler:
    @staticmethod
    def _validate_compiled_selectors(contract: CompletionContract) -> None:
        for pc in contract.postconditions:
            s = pc.selector
            if pc.provider == "github":
                if not s.get("repo") or s.get("kind") not in {"issue", "pull_request"}:
                    raise ValueError(f"compiled GitHub selector is incomplete for {pc.id}")
                number = s.get("number")
                if number is not None:
                    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                        raise ValueError(f"compiled GitHub selector has invalid number for {pc.id}")
                elif not any(
                    [s.get("title"), s.get("author"), s.get("head_ref") if s.get("kind") == "pull_request" else None]
                ):
                    raise ValueError(f"compiled GitHub discovery selector is too weak for {pc.id}")
            elif pc.provider == "gmail":
                if not s.get("message_id") and not any([s.get("subject"), s.get("to"), s.get("thread_id")]):
                    raise ValueError(f"compiled Gmail discovery selector is too weak for {pc.id}")
            elif pc.provider == "webhook":
                if not s.get("source") or not s.get("event_type"):
                    raise ValueError(f"compiled webhook selector is incomplete for {pc.id}")
