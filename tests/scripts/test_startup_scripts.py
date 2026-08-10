import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
POWERSHELL = "powershell.exe"


def _ps_literal(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            source,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _parse_script(path: Path) -> dict:
    command = f"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {_ps_literal(path)}, [ref]$tokens, [ref]$errors
)
$commands = @($ast.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.CommandAst]
}}, $true) | ForEach-Object {{
    [ordered]@{{
        name = $_.GetCommandName()
        elements = @($_.CommandElements | ForEach-Object {{ $_.Extent.Text }})
    }}
}})
[ordered]@{{
    errors = @($errors | ForEach-Object {{ $_.Message }})
    commands = $commands
    source = $ast.Extent.Text
}} | ConvertTo-Json -Compress -Depth 8
"""
    result = _run_powershell(command)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "name",
    ["run.ps1", "install_startup_task.ps1", "uninstall_startup_task.ps1"],
)
def test_startup_scripts_are_valid_powershell_with_no_dangerous_commands(name):
    parsed = _parse_script(SCRIPTS / name)

    assert parsed["errors"] == []
    command_names = {
        command["name"].casefold()
        for command in parsed["commands"]
        if command["name"] is not None
    }
    assert command_names.isdisjoint(
        {
            "invoke-expression",
            "iex",
            "start-process",
            "cmd.exe",
            "schtasks.exe",
            "invoke-webrequest",
            "invoke-restmethod",
            "remove-item",
            "stop-process",
            "get-process",
        }
    )


def test_run_script_uses_repo_local_python_logs_and_exact_local_uvicorn_command():
    parsed = _parse_script(SCRIPTS / "run.ps1")
    source = parsed["source"]
    command_elements = [
        element
        for command in parsed["commands"]
        for element in command["elements"]
    ]

    assert "$PSScriptRoot" in source
    assert "Split-Path" in source
    assert "Set-Location -LiteralPath $repoRoot" in source
    assert '".venv\\Scripts\\python.exe"' in source
    assert "Test-Path -LiteralPath $pythonExe -PathType Leaf" in source
    assert "New-Item -ItemType Directory -Force -LiteralPath $logsDirectory" in source
    assert [
        "-m",
        "uvicorn",
        "ticket_reviewer.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
    ] == [element.strip("'\"") for element in command_elements[-7:]]
    assert "exit $LASTEXITCODE" in source
    assert "0.0.0.0" not in source
    assert "::" not in source


def test_installer_registers_exact_limited_current_user_task_without_real_registration():
    script = _ps_literal(SCRIPTS / "install_startup_task.ps1")
    harness = f"""
$global:Captured = $null
function New-ScheduledTaskAction {{
    param($Execute, $Argument, $WorkingDirectory)
    [pscustomobject]@{{ Execute=$Execute; Argument=$Argument; WorkingDirectory=$WorkingDirectory }}
}}
function New-ScheduledTaskTrigger {{
    param([switch]$AtLogOn, $User)
    [pscustomobject]@{{ AtLogOn=[bool]$AtLogOn; User=$User }}
}}
function New-ScheduledTaskPrincipal {{
    param($UserId, $LogonType, $RunLevel)
    [pscustomobject]@{{ UserId=$UserId; LogonType=$LogonType; RunLevel=$RunLevel }}
}}
function New-ScheduledTaskSettingsSet {{
    param(
        [switch]$StartWhenAvailable,
        $MultipleInstances,
        [switch]$AllowStartIfOnBatteries,
        [switch]$DontStopIfGoingOnBatteries
    )
    [pscustomobject]@{{
        StartWhenAvailable=[bool]$StartWhenAvailable
        MultipleInstances=$MultipleInstances
        AllowStartIfOnBatteries=[bool]$AllowStartIfOnBatteries
        DontStopIfGoingOnBatteries=[bool]$DontStopIfGoingOnBatteries
    }}
}}
function Register-ScheduledTask {{
    param($TaskName, $TaskPath, $Action, $Trigger, $Principal, $Settings, [switch]$Force)
    $global:Captured = [ordered]@{{
        TaskName=$TaskName
        TaskPath=$TaskPath
        Action=$Action
        Trigger=$Trigger
        Principal=$Principal
        Settings=$Settings
        Force=[bool]$Force
    }}
}}
& {script}
$global:Captured | ConvertTo-Json -Compress -Depth 8
"""

    result = _run_powershell(harness)

    assert result.returncode == 0, result.stderr
    captured = json.loads(result.stdout.strip().splitlines()[-1])
    assert captured["TaskName"] == "TicketListingReviewer"
    assert captured["TaskPath"] == "\\"
    assert captured["Force"] is True
    assert captured["Action"]["Execute"] == "powershell.exe"
    expected_run = str((SCRIPTS / "run.ps1").resolve())
    assert captured["Action"]["Argument"] == (
        '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File '
        f'"{expected_run}"'
    )
    assert captured["Action"]["WorkingDirectory"] == str(ROOT.resolve())
    assert captured["Trigger"]["AtLogOn"] is True
    assert captured["Trigger"]["User"] == captured["Principal"]["UserId"]
    assert captured["Principal"]["LogonType"] == "Interactive"
    assert captured["Principal"]["RunLevel"] == "Limited"
    assert captured["Settings"] == {
        "StartWhenAvailable": True,
        "MultipleInstances": "IgnoreNew",
        "AllowStartIfOnBatteries": True,
        "DontStopIfGoingOnBatteries": True,
    }


@pytest.mark.parametrize("exists", [True, False])
def test_uninstaller_only_removes_exact_root_task_and_is_idempotent(exists):
    script = _ps_literal(SCRIPTS / "uninstall_startup_task.ps1")
    existing = "[pscustomobject]@{TaskName='TicketListingReviewer';TaskPath='\\'}" if exists else "$null"
    harness = f"""
$global:Lookups = @()
$global:Removals = @()
function Get-ScheduledTask {{
    param($TaskName, $TaskPath, $ErrorAction)
    $global:Lookups += ,@($TaskName, $TaskPath)
    return {existing}
}}
function Unregister-ScheduledTask {{
    param($TaskName, $TaskPath, [switch]$Confirm)
    $global:Removals += ,@($TaskName, $TaskPath, [bool]$Confirm)
}}
& {script}
[ordered]@{{ Lookups=$global:Lookups; Removals=$global:Removals }} |
    ConvertTo-Json -Compress -Depth 6
"""

    result = _run_powershell(harness)

    assert result.returncode == 0, result.stderr
    captured = json.loads(result.stdout.strip().splitlines()[-1])
    assert captured["Lookups"] == [["TicketListingReviewer", "\\"]]
    if exists:
        assert captured["Removals"] == [["TicketListingReviewer", "\\", False]]
    else:
        assert captured["Removals"] == []
    assert "SYSTEM" not in result.stdout
    assert str(ROOT.resolve()) not in result.stdout


def test_task_scripts_use_exact_names_without_wildcards_elevation_or_enumeration():
    install = _parse_script(SCRIPTS / "install_startup_task.ps1")
    uninstall = _parse_script(SCRIPTS / "uninstall_startup_task.ps1")
    combined = install["source"] + uninstall["source"]
    lowered = combined.casefold()

    assert combined.count('"TicketListingReviewer"') == 2
    assert "-TaskPath $taskPath" in combined
    assert 'taskPath = "\\"' in combined
    assert "*" not in combined
    assert "administrator" not in lowered
    assert "highest" not in lowered
    assert '-userid "system"' not in lowered
    assert "atstartup" not in lowered
    assert "runas" not in lowered
    assert "get-scheduledtask |" not in lowered
    assert "foreach" not in lowered


def test_importing_main_creates_no_database_services_scheduler_or_threads(tmp_path):
    probe = """
import json
from pathlib import Path
import threading
before = {thread.name for thread in threading.enumerate()}
from ticket_reviewer import main
after = {thread.name for thread in threading.enumerate()}
print(json.dumps({
    "new_threads": sorted(after - before),
    "files": sorted(str(path.relative_to(Path.cwd())) for path in Path.cwd().rglob("*")),
    "has_services": hasattr(main.app.state, "services"),
    "has_scheduler": hasattr(main.app.state, "scheduler"),
}))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), environment.get("PYTHONPATH", "")]
    )

    result = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-c", probe],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    captured = json.loads(result.stdout.strip().splitlines()[-1])
    assert captured == {
        "new_threads": [],
        "files": [],
        "has_services": False,
        "has_scheduler": False,
    }
