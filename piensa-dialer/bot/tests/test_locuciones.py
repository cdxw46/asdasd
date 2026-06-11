import asyncio
import os
import shutil
import subprocess

import pytest

from app.locuciones import LocutionStore


def _make_audio(path: str) -> None:
    # 1s 440Hz tone as an mp3-like input (use wav; ffmpeg converts either way).
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", path],
        check=True,
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_add_activate_delete(tmp_path):
    async def go():
        store = LocutionStore(str(tmp_path), lang="es")
        src = str(tmp_path / "tone.wav")
        _make_audio(src)

        loc = await store.add_from_file(src, "Aviso de prueba", role="cliente")
        assert loc.id in {l.id for l in store.list("cliente")}
        # first one becomes active automatically
        assert store.active("cliente").id == loc.id
        assert os.path.exists(os.path.join(str(tmp_path), f"{loc.stem}.wav"))
        assert store.active_media("cliente") == f"sound:custom/{loc.stem}"

        loc2 = await store.add_from_file(src, "Segundo", role="cliente")
        await store.set_active(loc2.id)
        assert store.active("cliente").id == loc2.id

        # persistence: a fresh store reads the index back
        store2 = LocutionStore(str(tmp_path), lang="es")
        assert {l.id for l in store2.list("cliente")} == {loc.id, loc2.id}
        assert store2.active("cliente").id == loc2.id

        assert await store.delete(loc2.id) is True
        assert store.active("cliente") is None or store.active("cliente").id != loc2.id

    asyncio.run(go())
