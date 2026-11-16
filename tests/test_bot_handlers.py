import sys
from types import ModuleType, SimpleNamespace

import pytest
from telegram import CallbackQuery, User


@pytest.mark.asyncio
async def test_handle_text_message_passes_authenticated_user_id(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    repository_module = ModuleType("autobill.db.repository")
    repository_module.BillRepository = type("BillRepository", (), {})
    monkeypatch.setitem(sys.modules, "autobill.db.repository", repository_module)
    pipeline_module = ModuleType("autobill.extract.pipeline")
    pipeline_module.ExtractionPipeline = type("ExtractionPipeline", (), {})
    monkeypatch.setitem(sys.modules, "autobill.extract.pipeline", pipeline_module)

    from autobill.bot import handlers
    from autobill.bot.state import UserState

    user_state = SimpleNamespace(state=UserState.EDITING_FIELD)
    monkeypatch.setattr(handlers.state_manager, "get_state", lambda _user_id: user_state)
    calls = []
    user_id = 42

    async def handle_edit_value(_update, _context, _state, user_id):
        calls.append(user_id)

    monkeypatch.setattr(handlers, "handle_edit_value", handle_edit_value)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id))

    await handlers.handle_text_message(update, object())
    query = CallbackQuery(
        id="callback",
        from_user=User(id=42, first_name="Test", is_bot=False),
        chat_instance="chat",
        data="review_next",
    )

    assert calls == [user_id]
    assert handlers.get_user_id(query) == user_id
