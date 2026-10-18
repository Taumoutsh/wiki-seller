import json
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
        telegram.state_file.write_text('{"offset": 1002, "asked": {"42": "2999-01-01T00:00:00+00:00"}}')
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


def wait_for(condition, seconds=10):
    import time

    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    return condition()


def test_update_command_asks_for_a_report(tmp_path):
    from wiki_seller.telegram import CommandListener

    requested = []
    with FakeTelegram() as fake:
        listener = CommandListener(bot(tmp_path, fake), lambda chat, kind: requested.append(kind)).start()
        try:
            assert wait_for(lambda: (tmp_path / "telegram.json").exists())  # écoute prête
            fake.say("/update")
            assert wait_for(lambda: requested == ["report"])
            fake.say("/ventes")
            assert wait_for(lambda: requested == ["report", "history"])
            fake.say("salut")
            assert wait_for(lambda: len(fake.sent) == 3)
            message_id = fake.say(COOKIES)  # aucune session demandée : supprimé
            assert wait_for(lambda: message_id in fake.deleted)
        finally:
            listener.stop()
    assert fake.sent[0].startswith("⏳ Bilan") and fake.sent[1].startswith("⏳ Historique")
    assert "/update" in fake.sent[2] and "/ventes" in fake.sent[2]
    assert "aucune session n'est demandée" in fake.sent[-1]


def test_session_reply_reaches_the_request_while_commands_are_listened(tmp_path):
    import threading

    from wiki_seller.telegram import CommandListener

    tried = []
    with FakeTelegram() as fake:
        telegram = bot(tmp_path, fake)
        listener = CommandListener(telegram, lambda chat, kind: None).start()
        try:
            assert wait_for(lambda: (tmp_path / "telegram.json").exists())

            def answer():
                assert wait_for(lambda: fake.sent)
                fake.say("/update")  # pendant une demande de session : rappel, pas de bilan
                fake.say(COOKIES)

            threading.Thread(target=answer, daemon=True).start()
            assert telegram.request_session("https://site", lambda c: tried.append(c) or True, timedelta(seconds=15))
        finally:
            listener.stop()
    assert len(tried) == 1 and len(fake.deleted) == 1
    assert any("envoyez d'abord" in t for t in fake.sent) and fake.sent[-1].startswith("✅")


def test_list_commands_edit_the_lists(tmp_path):
    from wiki_seller.lists import buy_file, safe_file, sell_file
    from wiki_seller.telegram import CommandListener

    from .test_end_to_end import make_config

    from wiki_seller import accounts

    base = make_config(tmp_path, "http://127.0.0.1:1", ["Tour Eiffel"], wanted=[], telegram_chat_id=CHAT_ID)
    accounts.migrate(base)
    config = accounts.account_config(base, CHAT_ID)
    commands = ["/safe Victor Hugo", "/want Gustave Eiffel 300 x2", "/buy", "/sell Tour Eiffel 800",
                "/buyandsell Apollo 11 500 900", "/unsell Pomme", "/unsafe Victor Hugo", "/buy Pomme x3",
                "/sell Loire", "/buyandsell Rhône 40", "/list"]
    with FakeTelegram() as fake:
        listener = CommandListener(bot(tmp_path, fake), lambda chat, kind: None, base).start()
        try:
            assert wait_for(lambda: (tmp_path / "telegram.json").exists())
            for n, command in enumerate(commands, 1):
                fake.say(command)
                assert wait_for(lambda: len(fake.sent) == n)
        finally:
            listener.stop()
    safe, want, buy_usage, sell, buy_and_sell, unsell, unsafe, buy_auto, sell_auto, buy_and_sell_auto, listing = fake.sent
    assert safe.startswith("🛡️ « Victor Hugo » protégée")
    assert "Gustave Eiffel » ajoutée dans les achats (300 max, 2 exemplaire(s))" in want and "/want est l'ancien" in want
    assert buy_usage.startswith("❌ Usage : /buy")
    assert "1 exemplaire(s) à 800" in sell and "Elle est protégée" in sell
    assert "Apollo 11 » ajoutée dans les achats (500 max, 1 exemplaire(s)), revendue à 900" in buy_and_sell
    assert unsell == "« Pomme » n'est pas dans les ventes."
    assert unsafe == "« Victor Hugo » n'est plus protégée."
    assert "Pomme » ajoutée dans les achats (max auto (90 % du marché), 3 exemplaire(s))" in buy_auto
    assert "1 exemplaire(s) à auto (110 % du marché)" in sell_auto
    assert "Rhône » ajoutée dans les achats (40 max, 1 exemplaire(s)), revendue à auto (110 % du marché)" \
        in buy_and_sell_auto
    assert listing.splitlines() == ["🛡️ Protégées (1) : Tour Eiffel", "🛒 À acheter (4) :",
                                    "• Gustave Eiffel : 300 max, 0/2 obtenu(s)",
                                    "• Apollo 11 : 500 max, 0/1 obtenu(s), revente à 900",
                                    "• Pomme : max auto (90 % du marché), 0/3 obtenu(s)",
                                    "• Rhône : 40 max, 0/1 obtenu(s), revente à auto (110 % du marché)",
                                    "💰 À vendre (2) :", "• Tour Eiffel : 800, 0/1 vendu(s)",
                                    "• Loire : auto (110 % du marché), 0/1 vendu(s)"]
    assert json.loads(safe_file(config).read_text()) == ["Tour Eiffel"]
    assert json.loads(buy_file(config).read_text()) == [
        {"name": "Gustave Eiffel", "max_price": 300, "copies": 2},
        {"name": "Apollo 11", "max_price": 500, "sell_price": 900},
        {"name": "Pomme", "max_price": "auto", "copies": 3},
        {"name": "Rhône", "max_price": 40, "sell_price": "auto"}]
    assert [(c["name"], c["price"]) for c in json.loads(sell_file(config).read_text())] == [
        ("Tour Eiffel", 800), ("Loire", "auto")]


def test_formatting_refused_by_telegram_falls_back_to_plain_text(tmp_path):
    with FakeTelegram() as fake:
        telegram = bot(tmp_path, fake)
        telegram.send("<b>Bilan</b>\n<pre>Loire &amp; Cher  40</pre>", html=True)
        fake.reject_html = True
        telegram.send("<b>Bilan</b>\n<pre>Loire &amp; Cher  40</pre>", html=True)
    assert fake.sent == ["<b>Bilan</b>\n<pre>Loire &amp; Cher  40</pre>", "Bilan\nLoire & Cher  40"]
    assert fake.parse_modes == ["HTML", None]
