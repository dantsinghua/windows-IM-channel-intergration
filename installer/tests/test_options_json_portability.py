"""Static Inno boundary guard; PS5.1 runtime tests independently validate parsing."""
from pathlib import Path
import re


def test_every_inno_string_option_uses_the_json_encoder():
    source = (Path(__file__).resolve().parents[1] / "engine/qtrade-setup-engine.iss").read_text(encoding="utf-8-sig")
    function = source.split("function BuildOptionsJson(): String;", 1)[1].split("function ReadLastLine", 1)[0]
    pairs = re.findall(r"S := S \+ '[^']*\"(\w+)\":' \+ JsonQuoted\(([^)]+)\)", function)
    actual = dict(pairs)
    expected = {
        "crash_dump_dir": "OptCrashDumpDir", "wechat_backup_dir": "OptWeChatBackupDir",
        "source_exe": "OptSourceExe", "restore_data": "OptRestoreData", "wechat_selected_path": "WeChatSelectedPath",
        "apk_url": "OptApkUrl", "docker_cidr": "OptDockerCidr", "mode": "OptMode",
    }
    assert expected.items() <= actual.items()
    assert not re.search(r"S := S \+ '[^']*' \+ (?:Opt\w+|WeChatSelectedPath)\s*\+", function)
