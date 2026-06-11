"""One-off REAL test call: dial a number, play the prompt, detect DTMF 1.

Runs against the already-running Asterisk via ARI on a separate Stasis app
("test") so it does not collide with the live bot's "outbound" app.

Usage: python testcall.py +34664721946
"""
import asyncio
import sys

from app.ari import ARIClient
from app.config import load_config

DEST = sys.argv[1] if len(sys.argv) > 1 else "+34664721946"


async def main() -> None:
    cfg = load_config()
    client = ARIClient(cfg.ari_rest_url, cfg.ari_username, cfg.ari_password, "test")
    done = asyncio.Event()
    state = {"channel": None, "pressed": None}

    async def on_event(ev: dict) -> None:
        et = ev.get("type")
        ch = (ev.get("channel") or {}).get("id")
        if et == "ChannelStateChange":
            print(f"  [state] {ch} -> {(ev.get('channel') or {}).get('state')}")
        elif et == "StasisStart":
            print(f"  [answered] {ch} -> playing prompt")
            state["channel"] = ch
            await client.answer(ch)
            await client.play(ch, cfg.sound_media, f"pb-{ch}")
        elif et == "ChannelDtmfReceived":
            digit = ev.get("digit")
            print(f"  [DTMF] received '{digit}'")
            if digit == "1":
                state["pressed"] = True
                print("  [OK] caller pressed 1 -> would transfer to agent now")
                await client.play(ch, "sound:custom/piensa-aviso", f"pbc-{ch}")
        elif et == "PlaybackFinished":
            pass
        elif et == "ChannelDestroyed":
            print(f"  [hangup] {ch} cause={ev.get('cause')} ({ev.get('cause_txt')})")
            done.set()

    await client.connect(on_event)
    await client.wait_until_ready()

    endpoint = f"PJSIP/{DEST.lstrip('+')}@{cfg.sip_endpoint}"
    print(f"Originating REAL call to {DEST} via {endpoint} (callerid {cfg.caller_id})…")
    try:
        await client.originate(
            endpoint=endpoint,
            channel_id="testcall-1",
            caller_id=cfg.caller_id,
            timeout=35,
            app_args="test",
        )
    except Exception as exc:  # noqa: BLE001
        print("ORIGINATE ERROR:", exc)
        await client.close()
        return

    try:
        await asyncio.wait_for(done.wait(), timeout=80)
    except asyncio.TimeoutError:
        print("  [timeout] no terminal event; hanging up")
        if state["channel"]:
            await client.hangup(state["channel"])
    await asyncio.sleep(1)
    await client.close()
    print(f"RESULT: pressed_1={state['pressed']}")


if __name__ == "__main__":
    asyncio.run(main())
