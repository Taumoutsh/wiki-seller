from datetime import timedelta

from wiki_seller.telegram import TelegramBot

from .fake_telegram import CHAT_ID, TOKEN, FakeTelegram

COOKIES = "sb-x-auth-token.0=base64-abc; sb-x-auth-token.1=def"


def bot(tmp_path, fake):
    return TelegramBot(TOKEN, CHAT_ID, tmp_path / "telegram.json", fake.url)


def test_bot_asks_for_the_session_and_accepts_the_reply(tmp_path):
    tried = []
    with FakeTelegram() as fake:
        fake.say("ancien message, envoyé avant la demande")
        telegram = bot(tmp_path, fake)

        def try_cookies(cookies):
            tried.append(cookies)
            return True

        # Les messages arrivent pendant l'attente : d'abord d'une autre conversation,
        # puis un texte sans cookie, puis la bonne ligne.
        import threading
        import time

        def answer():  # après la demande, comme une vraie personne
            while not fake.sent:
                time.sleep(0.05)
            fake.say(COOKIES, chat_id="999")
            fake.say("bonjour")
            fake.message_id = fake.say(COOKIES)

        threading.Thread(target=answer).start()
        assert telegram.request_session("https://www.wiki-masters.com", try_cookies, timedelta(seconds=10))
    assert tried == ["sb-x-auth-token.0=base64-abc; sb-x-auth-token.1=def"]  # seul le bon message
    assert "https://www.wiki-masters.com" in fake.sent[0]
    assert "pas reconnu" in fake.sent[1] and fake.sent[-1].startswith("✅")
    assert fake.deleted == [fake.message_id]  # le message avec les cookies est supprimé


def test_refused_session_is_reported_and_the_bot_keeps_waiting(tmp_path):
    answers = iter([False, True])
    with FakeTelegram() as fake:
        fake.say(COOKIES)  # envoyé avant la demande : ignoré
        telegram = bot(tmp_path, fake)
        telegram.state_file.write_text('{"offset": 1002, "asked_at": "2999-01-01T00:00:00+00:00"}')
        fake.say(COOKIES)
        fake.say(COOKIES)
        assert telegram.request_session("https://site", lambda c: next(answers), timedelta(seconds=10))
    assert fake.sent[0].startswith("❌") and fake.sent[1].startswith("✅")
    assert len(fake.deleted) == 2


def test_no_new_reminder_before_three_hours_and_timeout(tmp_path):
    with FakeTelegram() as fake:
        telegram = bot(tmp_path, fake)
        assert not telegram.request_session("https://site", lambda c: True, timedelta(seconds=1))
        assert not telegram.request_session("https://site", lambda c: True, timedelta(seconds=1))
    assert len(fake.sent) == 1  # une seule demande, la seconde attente reprend la même
