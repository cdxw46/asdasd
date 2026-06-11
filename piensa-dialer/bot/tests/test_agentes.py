import asyncio

from app.agentes import AgentStore


class FakeAMI:
    def __init__(self):
        self.reloads = 0

    async def reload_pjsip(self) -> bool:
        self.reloads += 1
        return True


def test_create_render_provision_delete(tmp_path):
    async def go():
        ami = FakeAMI()
        store = AgentStore(
            store_path=str(tmp_path / "agents.json"),
            include_path=str(tmp_path / "agents.conf"),
            ami=ami,
        )
        a = await store.create("Juan Pérez")
        assert a.sip_user.startswith("juanp") or a.sip_user.startswith("juan")
        assert len(a.sip_password) >= 10 and a.token
        assert ami.reloads == 1

        # include file rendered with endpoint, auth and aor
        conf = (tmp_path / "agents.conf").read_text()
        assert f"[{a.sip_user}]" in conf
        assert f"username = {a.sip_user}" in conf
        assert "type = aor" in conf

        # endpoints() used by the ring group
        assert f"PJSIP/{a.sip_user}" in store.endpoints()

        # provisioning XML carries the credentials + host
        xml = store.provisioning_xml(a, "1.2.3.4", transport=0)
        assert a.sip_user in xml and a.sip_password in xml and "1.2.3.4" in xml

        # linphone provisioning XML (self-hosted QR)
        lp = store.linphone_xml(a, "1.2.3.4", transport="udp")
        assert "lpconfig.xsd" in lp and a.sip_user in lp and a.sip_password in lp
        assert "transport=udp" in lp

        # lookup by token (used by the QR provisioning server)
        assert store.by_token(a.token).id == a.id

        # second agent gets a distinct username
        b = await store.create("Juan Pérez")
        assert b.sip_user != a.sip_user

        # persistence
        store2 = AgentStore(str(tmp_path / "agents.json"), str(tmp_path / "agents.conf"), ami)
        assert len(store2.list()) == 2

        assert await store.delete(a.id) is True
        assert a.id not in {x.id for x in store.list()}

    asyncio.run(go())


def test_seed_only_once(tmp_path):
    async def go():
        ami = FakeAMI()
        store = AgentStore(str(tmp_path / "a.json"), str(tmp_path / "a.conf"), ami)
        await store.ensure_seed("agente1", "secret123")
        await store.ensure_seed("agente1", "secret123")
        assert len(store.list()) == 1

    asyncio.run(go())
