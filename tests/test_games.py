"""Testes da aba Jogos (scan, filtros, segurança de ids/pastas). Rodar: python -m unittest discover -s tests -v"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.modules import games as g
from server.protocol import CommandError

LIBRARYFOLDERS = r'''"libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
		"apps" { "228980" "0" }
	}
	"1"
	{
		"path"		"D:\\Steam"
		"apps" { "2322010" "0" }
	}
}
'''

ACF = '''"AppState"
{
	"appid"		"%s"
	"name"		"%s"
	"StateFlags"		"%s"
	"installdir"		"%s"
}
'''


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ParseTests(unittest.TestCase):
    def test_library_paths_unescape(self):
        self.assertEqual(g.parse_library_paths(LIBRARYFOLDERS), [r"C:\Program Files (x86)\Steam", r"D:\Steam"])

    def test_vdf_pairs_keep_accents_and_escapes(self):
        d = g._vdf_pairs(ACF % ("2322010", "God of War Ragnarök", "4", "God of War Ragnarok"))
        self.assertEqual(d["name"], "God of War Ragnarök")
        self.assertEqual(g._vdf_pairs('"name"\t\t"A \\"B\\""')["name"], 'A "B"')

    def test_xbox_game_config(self):
        xml = '<Game><ShellVisuals DefaultDisplayName="Forza" Square150x150Logo="Logo.png"/></Game>'
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Logo.png").write_bytes(b"x")
            name, logo = g.parse_game_config(xml, d, "Microsoft.Forza")
            self.assertEqual((name, Path(logo).name), ("Forza", "Logo.png"))
        # nome de recurso (ms-resource:) cai para o nome do pacote
        name, _ = g.parse_game_config('<ShellVisuals DefaultDisplayName="ms-resource:Name"/>', "x", "Publisher.MyGameName")
        self.assertEqual(name, "My Game Name")


class SteamScanTests(unittest.TestCase):
    def test_scan_reads_all_libraries_and_filters_non_games(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, lib2 = Path(tmp) / "Steam", Path(tmp) / "D" / "Steam"
            write(root / "steamapps" / "libraryfolders.vdf", LIBRARYFOLDERS.replace(r"D:\\Steam", str(lib2).replace("\\", "\\\\")))
            write(root / "steamapps" / "appmanifest_228980.acf", ACF % ("228980", "Steamworks Common Redistributables", "4", "x"))
            write(root / "steamapps" / "appmanifest_1.acf", ACF % ("1", "Proton 9.0", "4", "Proton 9.0"))
            write(root / "steamapps" / "appmanifest_5.acf", ACF % ("5", "Half-baked", "1026", "Half"))  # baixando
            write(lib2 / "steamapps" / "appmanifest_2322010.acf", ACF % ("2322010", "God of War Ragnarök", "4", "God of War Ragnarok"))
            write(root / "appcache" / "librarycache" / "2322010" / "library_600x900.jpg", "img")
            games = g.scan_steam(root)
            self.assertEqual([x["id"] for x in games], ["steam-2322010"])
            game = games[0]
            self.assertEqual(game["name"], "God of War Ragnarök")
            self.assertEqual(game["launch"]["target"], "steam://rungameid/2322010")
            self.assertTrue(game["art"].endswith("library_600x900.jpg"))
            self.assertTrue(game["dir"].endswith(os.path.join("steamapps", "common", "God of War Ragnarok")))

    def test_no_steam(self):
        self.assertEqual(g.scan_steam(None), [])


class EpicScanTests(unittest.TestCase):
    def item(self, **over):
        d = {"DisplayName": "Fortnite", "AppName": "Fortnite", "CatalogNamespace": "fn", "CatalogItemId": "abc",
             "InstallLocation": r"D:\Epic\Fortnite", "LaunchExecutable": "FortniteLauncher.exe",
             "AppCategories": ["public", "games", "applications"]}
        d.update(over)
        return json.dumps(d)

    def test_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = Path(tmp)
            write(m / "a.item", self.item())
            write(m / "b.item", self.item(DisplayName="Unreal Engine", AppName="UE", AppCategories=["public", "applications"]))
            write(m / "c.item", self.item(AppName="Half", bIsIncompleteInstall=True))
            write(m / "d.item", "{ not json")
            games = g.scan_epic(m)
            self.assertEqual([x["id"] for x in games], ["epic-Fortnite"])
            self.assertEqual(games[0]["launch"]["target"],
                             "com.epicgames.launcher://apps/fn%3Aabc%3AFortnite?action=launch&silent=true")


class ExtraAndHideTests(unittest.TestCase):
    def test_extra_and_hide(self):
        extra = g.scan_extra({"extra": [
            {"label": "Valorant", "type": "app", "target": r"C:\Riot Games\Riot Client\RiotClientServices.exe",
             "args": ["--launch-product=valorant", "--launch-patchline=live"]},
            {"label": "Bad", "type": "weird", "target": "x"},
            {"label": "Site", "type": "url", "target": "https://x"}]})
        self.assertEqual([e["id"] for e in extra], ["extra-Valorant", "extra-Site"])
        self.assertEqual(extra[0]["launch"]["kind"], "exe")
        self.assertEqual(extra[1]["launch"]["kind"], "uri")
        self.assertEqual([e["id"] for e in g.apply_hide(extra, ["valo"])], ["extra-Site"])
        self.assertEqual([e["id"] for e in g.apply_hide(extra, ["extra-site"])], ["extra-Valorant"])


class SafeDirTests(unittest.TestCase):
    def test_rejects_dangerous_dirs(self):
        for bad in ("", None, "D:\\", "D:\\Games", r"C:\Windows", r"C:\Windows\System32", r"C:\Program Files",
                    r"C:\Program Files (x86)", os.path.expanduser("~"), "C:\\Users"):
            self.assertFalse(g.safe_dir(bad), bad)

    def test_accepts_game_dirs(self):
        for ok in (r"D:\Steam\steamapps\common\Game", r"C:\Program Files (x86)\Steam\steamapps\common\Game",
                   r"C:\XboxGames\Forza\Content"):
            self.assertTrue(g.safe_dir(ok), ok)

    def test_unsafe_dir_is_dropped_from_game(self):
        game = g.make_game("x", "X", "extra", {"kind": "uri", "target": "t"}, r"C:\Windows")
        self.assertEqual(game["dir"], "")

    def test_exe_under(self):
        self.assertTrue(g.exe_under(r"D:\Steam\steamapps\common\Game\bin\game.exe", r"D:\Steam\steamapps\common\Game"))
        self.assertTrue(g.exe_under(r"d:\steam\steamapps\common\game\GAME.EXE", r"D:\Steam\steamapps\common\Game"))
        # pasta irmã com o mesmo prefixo NÃO conta
        self.assertFalse(g.exe_under(r"D:\Steam\steamapps\common\Game2\g.exe", r"D:\Steam\steamapps\common\Game"))
        self.assertFalse(g.exe_under(None, r"D:\Steam\steamapps\common\Game"))


class ServiceTests(unittest.TestCase):
    def make(self, tmp):
        svc = g.Games({"games": {}}, data_dir=Path(tmp))
        svc.games = {
            "steam-1": g.make_game("steam-1", "Zelda", "steam", {"kind": "uri", "target": "steam://rungameid/1"},
                                   r"D:\Steam\steamapps\common\Zelda"),
            "epic-a": g.make_game("epic-a", "Alan Wake", "epic", {"kind": "uri", "target": "epic://a"},
                                  r"D:\Epic\Alan", icon_src=r"D:\Epic\Alan\a.exe"),
        }
        return svc

    def test_public_list_hides_secrets_and_sorts(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self.make(tmp)
            lst = svc.public_list()
            self.assertEqual([x["name"] for x in lst], ["Alan Wake", "Zelda"])
            for item in lst:
                self.assertEqual(set(item), {"id", "name", "store", "art", "recent"})
            svc.recent["steam-1"] = 5
            self.assertEqual(svc.public_list()[0]["id"], "steam-1")   # recentes primeiro

    def test_unknown_id_and_forged_path_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self.make(tmp)
            for bad in ("nope", r"C:\Windows\System32\cmd.exe", "../../x", None, {"id": "steam://x"}, 5):
                with self.assertRaises(CommandError):
                    svc.run(bad)
                with self.assertRaises(CommandError):
                    svc.close(bad)
            self.assertIsNone(svc.art("../../etc/passwd"))

    def test_run_launches_uri_and_records_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self.make(tmp)
            with mock.patch.object(g.os, "startfile", create=True) as sf, \
                    mock.patch.object(svc, "running_ids", return_value=[]):
                self.assertEqual(svc.run("steam-1"), "Zelda")
                sf.assert_called_once_with("steam://rungameid/1")
            self.assertIn("steam-1", json.loads((Path(tmp) / "games_state.json").read_text())["recent"])

    def test_run_refuses_when_already_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self.make(tmp)
            with mock.patch.object(g.os, "startfile", create=True) as sf, \
                    mock.patch.object(svc, "running_ids", return_value=["steam-1"]):
                with self.assertRaises(CommandError):
                    svc.run({"id": "steam-1"})
                sf.assert_not_called()

    def test_close_only_touches_procs_in_game_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self.make(tmp)

            class P:
                def __init__(self, exe):
                    self.info = {"exe": exe}
                    self.terminated = False

                def terminate(self):
                    self.terminated = True

                def kill(self):
                    pass

            inside = P(r"D:\Steam\steamapps\common\Zelda\z.exe")
            outside = P(r"D:\Steam\steamapps\common\Zelda2\z.exe")
            steam = P(r"C:\Program Files (x86)\Steam\steam.exe")
            import psutil
            with mock.patch.object(psutil, "process_iter", return_value=[inside, outside, steam]), \
                    mock.patch.object(psutil, "wait_procs", return_value=([inside], [])):
                self.assertEqual(svc.close("steam-1"), 1)
            self.assertTrue(inside.terminated)
            self.assertFalse(outside.terminated or steam.terminated)

    def test_close_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self.make(tmp)
            import psutil
            with mock.patch.object(psutil, "process_iter", return_value=[]):
                with self.assertRaises(CommandError):
                    svc.close("steam-1")

    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self.make(tmp)
            with mock.patch.object(g, "scan_all", return_value=list(svc.games.values())):
                self.assertEqual(svc.scan(), 2)
            again = g.Games({"games": {}}, data_dir=Path(tmp))
            self.assertEqual(sorted(again.games), ["epic-a", "steam-1"])


if __name__ == "__main__":
    unittest.main()
