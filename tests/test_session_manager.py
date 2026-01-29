"""Tests for the SessionManager conversation context."""

from src.voice_agent.session_manager import ConversationContext, SessionState


class TestConversationContext:
    def test_initial_state(self):
        ctx = ConversationContext()
        assert ctx.session_id == ""
        assert ctx.thread_id == ""
        assert ctx.turn_count == 0
        assert ctx.transcript_history == []

    def test_add_turn(self):
        ctx = ConversationContext()
        ctx.add_turn("customer", "Wo ist meine Bestellung?")
        ctx.add_turn("agent", "Ich schaue das für Sie nach.")

        assert ctx.turn_count == 2
        assert len(ctx.transcript_history) == 2
        assert ctx.transcript_history[0]["role"] == "customer"
        assert ctx.transcript_history[1]["role"] == "agent"
        assert ctx.transcript_history[0]["turn"] == 1
        assert ctx.transcript_history[1]["turn"] == 2


class TestSessionState:
    def test_states_exist(self):
        assert SessionState.IDLE.value == "idle"
        assert SessionState.CONNECTING.value == "connecting"
        assert SessionState.ACTIVE.value == "active"
        assert SessionState.PROCESSING.value == "processing"
        assert SessionState.DISCONNECTED.value == "disconnected"
