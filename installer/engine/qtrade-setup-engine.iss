; ============================================================================
;  QTrade 安装引擎(Inno Setup 6)—— 两段式外壳的**内层**
;  规格:docs/03 §2.1(两段式分工;引擎 ≈15 MB、无载荷)、§2.3(状态机)、§3.4(命令行 /QT_* 与退出码)、
;        §4(向导页与文案)、§5(失败回滚)、§2.14(卸载)
;
;  🔴 硬约束
;   1. 退出码必须与 docs/03 §3.4 表**逐字一致**;installer/tests/test_spec_consistency.py 会从文档解析该表比对本文件。
;   2. `/QT_*` 开关必须与 §3.4 命令行块**逐字一致**(同一测试比对)。
;   3. 引擎自身**不写任何防火墙规则**(§6):建规则只走 WinAgent `POST /wa/v1/firewall/ensure`。
;   4. `wsl --shutdown` 只在用户按下【现在切换】或带 `/QT_ACCEPT_SHUTDOWN=1` 时才发生(红线 6)。
;   5. 本文件必须 UTF-8 **with BOM**(Inno 6 读非 ASCII 的要求;与载荷内 ps1 的 W1 同口径)。
;
;  编译:ISCC.exe qtrade-setup-engine.iss  → Output\qtrade-setup-engine.exe
; ============================================================================

#define EngineName       "QTrade 安装程序"
#define EngineVersion    "1.0.0"
#define EnginePublisher  "QTrade"
#define EngineExeName    "qtrade-setup-engine"

[Setup]
AppId={{9B2F1C64-2F1E-4E1A-9A1B-0C7A5E3D1B02}
AppName={#EngineName}
AppVersion={#EngineVersion}
AppPublisher={#EnginePublisher}
VersionInfoVersion={#EngineVersion}
; 🔴 解压目录固定 %ProgramData%\QTrade(基线 §4;SFX 的 -o 写死,用户不可选——载荷路径被 .wslconfig / 服务 / 计划任务引用)
DefaultDirName={commonappdata}\QTrade
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableWelcomePage=no
; PrivilegesRequired=admin 保证 §2.4.1 的「管理员」一项已成立(非管理员且 UAC 拒绝 → 直接退 22)
PrivilegesRequired=admin
; 引擎编 64 位(§2.15:32 位进程里 PROCESSOR_ARCHITEW6432 才是真值,编 64 位避免这个坑)
; 🔴 这里用 `x64os` 而**不是**编译器建议的 `x64compatible`:
;    §2.4.1 明写 ARM64 → `E_INSTALL_NOT_X64`(redroid 的 x86_64 镜像 + ndk_translation **只在 x86_64 上成立**)。
;    `x64compatible` 会把「能跑 x64 模拟的 ARM64」也放进来,正好放进我们必须拒绝的那一类;
;    `x64os` 才是「真正的 x64 操作系统」。编译器那句 "preferred in most cases" 对本产品不成立。
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
OutputBaseFilename={#EngineExeName}
Compression=lzma2/max
SolidCompression=yes
SetupLogging=yes
Uninstallable=yes
UninstallDisplayName=QTrade
WizardStyle=modern
; 引擎自身不含载荷(载荷由外层 7z 归档承载,§2.1)
DiskSpanning=no

[Languages]
; 🔴 界面文案仅中文。Inno Setup 6 的官方发行版**不一定自带** ChineseSimplified.isl
;    (本机 6.x 的 Languages\ 里就没有),所以把它**随仓库走**:installer\engine\lang\。
;    来源 = Inno 官方 issrc 仓库 Files/Languages/ChineseSimplified.isl(官方语言,非 Unofficial)。
;    路径相对 .iss 所在目录,换一台机器不装中文包也能编。
Name: "zh"; MessagesFile: "lang\ChineseSimplified.isl"

[Messages]
; 只覆盖**产品相关**的几条;按钮、通用提示等一律用 ChineseSimplified.isl 的官方译文,
; 免得自己维护一份会漂移的半吊子中文。
SetupAppTitle=QTrade 安装程序
SetupWindowTitle=QTrade 安装程序
WelcomeLabel1=欢迎安装 QTrade
FinishedHeadingLabel=QTrade 安装完成

[Files]
; 引擎自带的 PowerShell 模块与派发器(§2.1:引擎 = Inno [Code] + PowerShell 模块)
; 🔴 全部 ps1/psm1 必须 UTF-8 with BOM(§2.6.7 W1),由 build.ps1 在打包前逐个检查
Source: "run-step.ps1";     DestDir: "{app}\install\engine";         Flags: ignoreversion
Source: "lang\*.isl";       DestDir: "{app}\install\engine\lang";    Flags: ignoreversion
Source: "modules\*.psm1";   DestDir: "{app}\install\engine\modules"; Flags: ignoreversion

[UninstallRun]
; §2.14:卸载指向落盘引擎;`/QT_KEEP_DATA` 缺省 1(保留数据)。
; RunOnceId 保证这条在卸载过程中**只执行一次** —— 卸载本身是可重入的(§2.14 的每步都带判据),
; 但重复跑一遍 `--unregister`/删服务只会刷一堆「对象不存在」的噪声,没必要。
Filename: "{app}\install\engine\{#EngineExeName}.exe"; Parameters: "/QT_MODE=uninstall"; Flags: runhidden; RunOnceId: "QTradeUninstallEngine"

[Code]
// ───────────────────────────────────────────────────────────────────────────
// 退出码表 —— 🔴 逐字对应 docs/03 §3.4。改这里之前先改文档。
// `FAILED:<步名>:<原因码>` 里的原因码 = 下表名去掉 `E_INSTALL_` 前缀。
// ───────────────────────────────────────────────────────────────────────────
const
  OK                                      = 0;
  E_INSTALL_WAIT_USER                     = 10;
  E_INSTALL_REBOOT_REQUIRED               = 3010;
  E_INSTALL_WIN_TOO_OLD                   = 20;
  E_INSTALL_NOT_X64                       = 21;
  E_INSTALL_NOT_ADMIN                     = 22;
  E_INSTALL_ELEVATED_AS_OTHER_USER        = 23;
  E_INSTALL_VIRT_DISABLED                 = 24;
  E_INSTALL_POLICY_BLOCKED                = 25;
  E_INSTALL_DISK_LOW                      = 26;
  E_INSTALL_MEM_LOW                       = 27;
  E_INSTALL_OTHER_CUSTOM_KERNEL_DECLINED  = 28;
  E_INSTALL_ALREADY_RUNNING               = 29;
  E_INSTALL_PAYLOAD_CORRUPT               = 30;
  E_INSTALL_ACL_HARDEN_FAILED             = 31;  // 安装根/内核文件 ACL 收紧失败(阻断)
  E_INSTALL_FEATURE_ENABLE_FAILED         = 40;
  E_INSTALL_RESUME_ENGINE_MISSING         = 41;
  E_INSTALL_WSL_MSI_FAILED                = 50;
  E_INSTALL_WSL_BROKEN                    = 51;
  E_INSTALL_KERNEL_SHA_MISMATCH           = 60;
  E_INSTALL_WSLCONFIG_PARSE_FAILED        = 61;
  E_INSTALL_KERNEL_BOOT_TIMEOUT           = 62;
  E_INSTALL_KERNEL_BOOT_FAILED            = 63;
  E_INSTALL_KERNEL_NO_BINDER              = 64;
  E_INSTALL_KERNEL_ROLLBACK_FAILED        = 65;
  E_INSTALL_KERNEL_SHUTDOWN_TIMEOUT       = 66;
  E_INSTALL_KCHECK_IMPORT_FAILED          = 67;
  E_INSTALL_DISTRO_NAME_CONFLICT_DECLINED = 70;
  E_INSTALL_IMPORT_FAILED                 = 71;
  E_INSTALL_SYSTEMD_NOT_READY             = 72;
  E_INSTALL_DOCKER_NOT_READY              = 73;
  E_INSTALL_IMAGE_LOAD_FAILED             = 74;
  E_INSTALL_AGENT_NOT_READY               = 75;
  E_INSTALL_DOCKER_CIDR_EXHAUSTED         = 76;
  E_INSTALL_VCREDIST_FAILED               = 80;
  E_INSTALL_SERVICE_INSTALL_FAILED        = 81;
  E_INSTALL_WINAGENT_NOT_READY            = 82;
  E_INSTALL_WECHAT_BACKUP_FAILED          = 90;
  E_INSTALL_WECHAT_REINSTALL_FAILED       = 91;
  E_INSTALL_SELFTEST_REDROID_BOOT         = 100;
  E_INSTALL_SELFTEST_AGENT                = 101;
  E_INSTALL_SELFTEST_WINAGENT             = 102;
  E_INSTALL_UPGRADE_DATA_BACKUP_FAILED    = 120;
  E_INSTALL_UNINSTALL_PARTIAL             = 121;
  E_INSTALL_DOWNGRADE_REFUSED             = 122;
  E_INSTALL_DISK_FULL                     = 123;
  E_INSTALL_INTERNAL                      = 200;

// 状态机键名(基线 §8.2 / docs/03 §2.3)
const
  ST_PRECHECK           = 'PRECHECK';
  ST_PAYLOAD_STAGED     = 'PAYLOAD_STAGED';
  ST_WSL_FEATURE        = 'WSL_FEATURE';
  ST_REBOOT_PENDING     = 'REBOOT_PENDING';
  ST_WSL_MSI            = 'WSL_MSI';
  ST_KERNEL_STAGED      = 'KERNEL_STAGED';
  ST_WSLCONFIG_WRITTEN  = 'WSLCONFIG_WRITTEN';
  ST_KERNEL_VERIFIED    = 'KERNEL_VERIFIED';
  ST_KERNEL_ROLLED_BACK = 'KERNEL_ROLLED_BACK';
  ST_DISTRO_IMPORTED    = 'DISTRO_IMPORTED';
  ST_IMAGES_LOADED      = 'IMAGES_LOADED';
  ST_WINAGENT_INSTALLED = 'WINAGENT_INSTALLED';
  ST_CLIENTS_CHECKED    = 'CLIENTS_CHECKED';
  ST_SELFTEST_OK        = 'SELFTEST_OK';
  ST_DONE               = 'DONE';

// 🔴 命令行开关 —— 逐字对应 docs/03 §3.4 的命令行块
const
  SW_QT_MODE                 = '/QT_MODE=';                 // install|resume|upgrade|repair|uninstall|verify-kernel
  SW_QT_ACCEPT_SHUTDOWN      = '/QT_ACCEPT_SHUTDOWN=';      // 静默模式下允许 wsl --shutdown(§2.6.3;P-24 算显式确认)
  SW_QT_ACCEPT_REBOOT        = '/QT_ACCEPT_REBOOT=';        // 静默模式下允许立即重启 Windows;缺省 → 3010
  SW_QT_WSL_MEMORY           = '/QT_WSL_MEMORY=';           // 覆盖 04 [wsl] memory_by_physical 查表值(B-5)
  SW_QT_KEEP_WSL_MEMORY      = '/QT_KEEP_WSL_MEMORY=';      // 已有 memory= 一律不改、只提示(退回 R3)
  SW_QT_CRASH_DUMPS          = '/QT_CRASH_DUMPS=';          // .wslconfig maxCrashDumpCount(R-10;缺省 2,0=不留)
  SW_QT_CRASH_DUMP_DIR       = '/QT_CRASH_DUMP_DIR=';       // crashDumpFolder 目标(R-10)
  SW_QT_WECHAT_HOSTS_BLOCK   = '/QT_WECHAT_HOSTS_BLOCK=';   // 不写 hosts 屏蔽行(B-2 的 B 层;缺省 1)
  SW_QT_WECHAT               = '/QT_WECHAT=';               // check|skip|reinstall(静默时 reinstall 降级 check,P-19)
  SW_QT_WECHAT_BACKUP_DIR    = '/QT_WECHAT_BACKUP_DIR=';    // 微信备份目录
  SW_QT_WECHAT_BACKUP_MODE   = '/QT_WECHAT_BACKUP_MODE=';   // copy|rename|skip(§2.9.3 第 3 步)
  SW_QT_APK_URL              = '/QT_APK_URL=';              // → 02 [runtime] apk_url(C-43 唯一出处)
  SW_QT_DOCKER_CIDR          = '/QT_DOCKER_CIDR=';          // 覆盖 §2.7.3 自动选段(仍做路由冲突校验,冲突即拒 → 76)
  SW_QT_REPLACE_OTHER_KERNEL = '/QT_REPLACE_OTHER_KERNEL='; // 静默下同意替换 OTHER_CUSTOM 内核;缺省 → 28
  SW_QT_KEEP_DATA            = '/QT_KEEP_DATA=';            // 卸载时保留/删除数据(缺省 1)
  SW_QT_FROM_SFX             = '/QT_FROM_SFX=';             // §2.1:由自解压外壳拉起
  SW_QT_SOURCE_EXE           = '/QT_SOURCE_EXE=';           // §2.1:外壳自身路径(只记 resume.source_exe 做提示)
  // §2.13 末的 `--restore-data <tar>` 是**成对参数**(规格原文 `/QT_MODE=repair --restore-data <tar>`),
  // 不是 /QT_* 开关 —— 按规格逐字实现,不自造开关名。
  ARG_RESTORE_DATA           = '--restore-data';

// Inno 原生退出码只有 0~8;本引擎必须把 §3.4 的码**原样透传**给 SFX 外壳,
// 故用 kernel32 的 ExitProcess 强制设置进程退出码。
procedure ExitProcess(uExitCode: Cardinal);
  external 'ExitProcess@kernel32.dll stdcall';

// §2.12 并发保护:命名互斥体 `Global\QTradeSetup`,第二个实例直接退出 29。
// 🔴 必须由**引擎进程本身**持有 —— 放进 PowerShell 子进程里的话,那个进程一跑完就退出、
//    互斥体随之释放,等于没有保护(验收 M1-21 会红)。
function CreateMutexW(lpMutexAttributes: Cardinal; bInitialOwner: Boolean; lpName: String): Cardinal;
  external 'CreateMutexW@kernel32.dll stdcall';
function GetLastError(): Cardinal;
  external 'GetLastError@kernel32.dll stdcall';

const
  ERROR_ALREADY_EXISTS = 183;

var
  OptMode: String;
  OptAcceptShutdown: Boolean;
  OptAcceptReboot: Boolean;
  OptWslMemory: String;
  OptKeepWslMemory: Boolean;
  OptCrashDumps: String;
  OptCrashDumpDir: String;
  OptWeChatHostsBlock: Boolean;
  OptWeChat: String;
  OptWeChatBackupDir: String;
  OptWeChatBackupMode: String;
  OptApkUrl: String;
  OptDockerCidr: String;
  OptReplaceOtherKernel: Boolean;
  OptKeepData: Boolean;
  OptFromSfx: Boolean;
  OptSourceExe: String;

  OptRestoreData: String;

  PageKernelConfirm: TInputOptionWizardPage;
  PageWeChat: TInputOptionWizardPage;
  LastStepJson: String;
  LastStepExit: Integer;
  AckKernelImpact: Boolean;
  DistroConflictChoice: String;    // '' | 'unregister' | 'cancel'
  PageWeChatSelect: TInputOptionWizardPage;
  WeChatMultiple: Boolean;
  WeChatInstallPaths: TArrayOfString;
  WeChatSelectedPath: String;
  WeChatUninstallConfirmed: Boolean;
  WeChatUserApproved: Boolean;
  WeChatBackupModeSel: String;     // auto | copy | rename | skip
  AcceptSchemaBreaking: Boolean;

// ── 命令行解析 ─────────────────────────────────────────────────────────────
function GetSwitchValue(const Prefix: String; const Default: String): String;
var
  I: Integer;
  S: String;
begin
  Result := Default;
  for I := 1 to ParamCount do
  begin
    S := ParamStr(I);
    if (Length(S) >= Length(Prefix)) and
       (CompareText(Copy(S, 1, Length(Prefix)), Prefix) = 0) then
    begin
      Result := Copy(S, Length(Prefix) + 1, Length(S));
      Exit;
    end;
  end;
end;

function GetSwitchBool(const Prefix: String; const Default: Boolean): Boolean;
var
  V: String;
begin
  if Default then V := GetSwitchValue(Prefix, '1') else V := GetSwitchValue(Prefix, '0');
  Result := (V = '1') or (CompareText(V, 'true') = 0) or (CompareText(V, 'yes') = 0);
end;

// 读「--name value」这种成对参数(§2.13 的 --restore-data);没给回空串。
function GetPairedArgValue(const Name: String): String;
var
  I: Integer;
begin
  Result := '';
  for I := 1 to ParamCount - 1 do
    if CompareText(ParamStr(I), Name) = 0 then
    begin
      Result := ParamStr(I + 1);
      Exit;
    end;
end;

function IsSilentRun(): Boolean;
begin
  Result := WizardSilent();
end;

procedure ParseCommandLine();
begin
  OptMode               := GetSwitchValue(SW_QT_MODE, 'install');
  OptAcceptShutdown     := GetSwitchBool(SW_QT_ACCEPT_SHUTDOWN, False);
  OptAcceptReboot       := GetSwitchBool(SW_QT_ACCEPT_REBOOT, False);
  OptWslMemory          := GetSwitchValue(SW_QT_WSL_MEMORY, '');
  OptKeepWslMemory      := GetSwitchBool(SW_QT_KEEP_WSL_MEMORY, False);
  OptCrashDumps         := GetSwitchValue(SW_QT_CRASH_DUMPS, '2');
  OptCrashDumpDir       := GetSwitchValue(SW_QT_CRASH_DUMP_DIR, '');
  OptWeChatHostsBlock   := GetSwitchBool(SW_QT_WECHAT_HOSTS_BLOCK, True);
  OptWeChat             := GetSwitchValue(SW_QT_WECHAT, 'check');
  OptWeChatBackupDir    := GetSwitchValue(SW_QT_WECHAT_BACKUP_DIR, '');
  OptWeChatBackupMode   := GetSwitchValue(SW_QT_WECHAT_BACKUP_MODE, 'auto');
  OptApkUrl             := GetSwitchValue(SW_QT_APK_URL, '');
  OptDockerCidr         := GetSwitchValue(SW_QT_DOCKER_CIDR, '');
  OptReplaceOtherKernel := GetSwitchBool(SW_QT_REPLACE_OTHER_KERNEL, False);
  OptKeepData           := GetSwitchBool(SW_QT_KEEP_DATA, True);
  OptFromSfx            := GetSwitchBool(SW_QT_FROM_SFX, False);
  OptSourceExe          := GetSwitchValue(SW_QT_SOURCE_EXE, '');
  OptRestoreData        := GetPairedArgValue(ARG_RESTORE_DATA);
  WeChatBackupModeSel   := OptWeChatBackupMode;

  // 🔴 P-19:`reinstall` 在静默模式下不可用 → 降级为 `check`(重装靠引导手动点)
  if IsSilentRun() and (CompareText(OptWeChat, 'reinstall') = 0) then
    OptWeChat := 'check';
end;

// ── 极简 JSON 取值(run-step.ps1 的输出是一行扁平 JSON,无需完整解析器)──────
function JsonStr(const Json, Key: String): String;
var
  P, Q: Integer;
  Pat: String;
begin
  Result := '';
  Pat := '"' + Key + '":"';
  P := Pos(Pat, Json);
  if P = 0 then Exit;
  P := P + Length(Pat);
  Q := P;
  while (Q <= Length(Json)) and (Json[Q] <> '"') do Q := Q + 1;
  Result := Copy(Json, P, Q - P);
end;

function JsonInt(const Json, Key: String; const Default: Integer): Integer;
var
  P, Q: Integer;
  Pat, S: String;
begin
  Result := Default;
  Pat := '"' + Key + '":';
  P := Pos(Pat, Json);
  if P = 0 then Exit;
  P := P + Length(Pat);
  Q := P;
  while (Q <= Length(Json)) and (Pos(Json[Q], '-0123456789') > 0) do Q := Q + 1;
  S := Copy(Json, P, Q - P);
  if S <> '' then Result := StrToIntDef(S, Default);
end;

function JsonBool(const Json, Key: String): Boolean;
var
  P: Integer;
  Pat: String;
begin
  Pat := '"' + Key + '":true';
  P := Pos(Pat, Json);
  Result := P > 0;
end;

// ── 调 PowerShell 派发器 ───────────────────────────────────────────────────
function BuildOptionsJson(): String;
var
  S: String;
begin
  S := '{';
  S := S + '"mode":"' + OptMode + '"';
  if OptAcceptShutdown then S := S + ',"accept_shutdown":true' else S := S + ',"accept_shutdown":false';
  if OptAcceptReboot then S := S + ',"accept_reboot":true' else S := S + ',"accept_reboot":false';
  if OptKeepWslMemory then S := S + ',"keep_wsl_memory":true' else S := S + ',"keep_wsl_memory":false';
  if OptReplaceOtherKernel then S := S + ',"replace_other_kernel":true' else S := S + ',"replace_other_kernel":false';
  if OptKeepData then S := S + ',"keep_data":true' else S := S + ',"keep_data":false';
  if OptWeChatHostsBlock then S := S + ',"wechat_hosts_block":true' else S := S + ',"wechat_hosts_block":false';
  S := S + ',"wsl_memory":"' + OptWslMemory + '"';
  S := S + ',"crash_dumps":"' + OptCrashDumps + '"';
  S := S + ',"crash_dump_dir":"' + OptCrashDumpDir + '"';
  S := S + ',"wechat":"' + OptWeChat + '"';
  S := S + ',"wechat_backup_dir":"' + OptWeChatBackupDir + '"';
  S := S + ',"wechat_backup_mode":"' + OptWeChatBackupMode + '"';
  S := S + ',"apk_url":"' + OptApkUrl + '"';
  S := S + ',"docker_cidr":"' + OptDockerCidr + '"';
  S := S + ',"source_exe":"' + OptSourceExe + '"';
  S := S + ',"package_version":"' + '{#EngineVersion}' + '"';
  S := S + ',"restore_data":"' + OptRestoreData + '"';
  S := S + ',"distro_conflict_choice":"' + DistroConflictChoice + '"';
  S := S + ',"wechat_backup_mode":"' + WeChatBackupModeSel + '"';
  S := S + ',"wechat_selected_path":"' + WeChatSelectedPath + '"';
  if WeChatUninstallConfirmed then S := S + ',"wechat_uninstall_confirmed":true' else S := S + ',"wechat_uninstall_confirmed":false';
  if IsSilentRun() then S := S + ',"silent":true' else S := S + ',"silent":false';
  if WeChatUserApproved then S := S + ',"wechat_user_approved":true' else S := S + ',"wechat_user_approved":false';
  if AcceptSchemaBreaking then S := S + ',"accept_schema_breaking":true' else S := S + ',"accept_schema_breaking":false';
  // `[wechat] silent_setup` 恒 false(P-19/B-3:微信安装器静默参数实测通过前,方案①恒交互)
  S := S + ',"wechat_silent_setup":false';
  S := S + ',"napcat_check":true';
  S := S + ',"diag_include_dmesg":true';
  S := S + '}';
  Result := S;
end;

function ReadLastLine(const FileName: String): String;
var
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := '';
  if not LoadStringsFromFile(FileName, Lines) then Exit;
  for I := GetArrayLength(Lines) - 1 downto 0 do
    if Trim(Lines[I]) <> '' then
    begin
      Result := Trim(Lines[I]);
      Exit;
    end;
end;

// 跑一个步骤。返回 True = ok;LastStepExit 带回 §3.4 的退出码,LastStepJson 带回整行结果。
function RunStep(const StepName: String): Boolean;
var
  OutFile, OptFile, Cmd, Params: String;
  OptLines: TArrayOfString;
  Code: Integer;
begin
  OutFile := ExpandConstant('{tmp}\qt-step-') + StepName + '.json';
  // 选项经**临时文件**传入,不走命令行 —— JSON 里的双引号穿 cmd /c 的引号规则极易被弄坏(同 W7 的教训)
  OptFile := ExpandConstant('{tmp}\qt-options.json');
  // ⚠️ 用 SaveStringsToUTF8File(复数)——单数的 SaveStringToUTF8File 在 Inno 6 里不存在;
  //    而 SaveStringToFile 在 Unicode Inno 下按 ANSI 写,路径里有中文就会写坏。
  //    它写出的 BOM 由 PowerShell 的 [IO.File]::ReadAllText 自动识别并吃掉,无需额外处理。
  SetArrayLength(OptLines, 1);
  OptLines[0] := BuildOptionsJson();
  SaveStringsToUTF8File(OptFile, OptLines, False);
  Cmd := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  // 🔴 ps1 一律 `-ExecutionPolicy Bypass -File` 调用(§2.2.3);同时对 ps1 做 Authenticode 签名,AllSigned 机器也能跑
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
            ExpandConstant('{app}\install\engine\run-step.ps1') + '"' +
            ' -Step ' + StepName +
            ' -Root "' + ExpandConstant('{app}') + '"' +
            ' -OptionsPath "' + OptFile + '"' +
            ' > "' + OutFile + '"';
  // 经 cmd /c 重定向 stdout,免得 Inno 拿不到输出
  if not Exec(ExpandConstant('{cmd}'), '/c ""' + Cmd + '" ' + Params + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) then
  begin
    LastStepExit := E_INSTALL_INTERNAL;
    LastStepJson := '';
    Result := False;
    Exit;
  end;
  LastStepExit := Code;
  LastStepJson := ReadLastLine(OutFile);
  Result := (Code = OK) or (Code = E_INSTALL_REBOOT_REQUIRED);
end;

// §2.6.3 的确认页要列出「当前检测到正在运行的发行版」与 Docker Desktop 状态。
function GetRunningDistros(): String;
var
  OutFile, Cmd: String;
  Code: Integer;
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := '';
  OutFile := ExpandConstant('{tmp}\qt-running.txt');
  Cmd := '/c "set WSL_UTF8=1 && "' + ExpandConstant('{sys}\wsl.exe') + '" --list --running --quiet > "' + OutFile + '""';
  if not Exec(ExpandConstant('{cmd}'), Cmd, '', SW_HIDE, ewWaitUntilTerminated, Code) then Exit;
  if not LoadStringsFromFile(OutFile, Lines) then Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Trim(Lines[I]) <> '' then
    begin
      if Result <> '' then Result := Result + '、';
      Result := Result + Trim(Lines[I]);
    end;
  if Result = '' then Result := '(无)';
end;

procedure FailWith(const Code: Integer; const Msg: String);
var
  Answer: Integer;
begin
  if not IsSilentRun() then
  begin
    // §5.3:每个失败页带日志路径与【导出诊断包】。诊断包**不含** vault / 密码 / 微信数据 / 消息正文(红线 2)。
    Answer := MsgBox(Msg + #13#10#13#10 +
      '日志:' + ExpandConstant('{app}\logs\') + #13#10#13#10 +
      '要导出一份诊断包吗?它只包含安装日志、.wslconfig、WSL 状态与策略键,' +
      '不包含任何密码、凭据、微信数据或聊天内容。',
      mbCriticalError, MB_YESNO);
    if Answer = IDYES then
    begin
      RunStep('diag');
      if LastStepJson <> '' then
        MsgBox('诊断包已导出:' + #13#10 + JsonStr(LastStepJson, 'message'), mbInformation, MB_OK);
    end;
  end;
  ExitProcess(Code);
end;

// ── 向导页(§4;🔴 C-39:安装器向导是 Inno 自己的页面,不是控制台的 P-SETUP)──
procedure InitializeWizard();
begin
  // 欢迎与告知(§4 第一行文案)
  WizardForm.WelcomeLabel2.Caption :=
    'QTrade 将安装:WSL2 组件、QTrade 专用内核(会替换本机 WSL 内核,影响你已有的 WSL 发行版与 Docker Desktop)、' +
    '发行版 qtrade、WinAgent 服务与用户会话代理、控制台。全程离线。预计 10–20 分钟,可能需要重启一次。' + #13#10#13#10 +
    '至少预留 16 GB(建议 20 GB)磁盘;QTrade 的 WSL 磁盘(ext4.vhdx)只增不减,删数据不还盘,' +
    '只有卸载注销发行版才释放。装好后可在控制台「资源监控」页查看整机与本程序的内存/CPU/磁盘占用。' + #13#10#13#10 +
    'QTrade 的 WSL 环境只在当前安装账号登录后运行,开机后请登录该账号(可锁屏,不要注销)。';

  PageKernelConfirm := CreateInputOptionPage(wpWelcome,
    '内核替换告知', '这一步会关闭 WSL 里正在运行的所有程序',
    '接下来要把 WSL 切换到 QTrade 的内核。这一步相当于给 WSL 重启一次(你的 Ubuntu 终端、' +
    'Docker Desktop 里的容器、正在编辑的文件都会被关闭)。切换后会自动验证,验证不通过会自动恢复原来的内核。',
    False, False);
  PageKernelConfirm.Add('我已知晓内核替换的影响,同意在安装过程中切换 WSL 内核');

  PageWeChat := CreateInputOptionPage(PageKernelConfirm.ID,
    '微信通道(可选)', '是否授权由本软件管理微信版本',
    '启用微信通道需要把微信换成随包的 4.1.12.26 版本(覆盖安装,不卸载、不动聊天记录)。' +
    '同时会在 hosts 里追加带标记的行屏蔽微信更新下载域名,卸载时成对删除。不勾选 = 跳过微信通道。',
    False, False);
  PageWeChat.Add('同意由本软件管理微信版本(等价 /QT_WECHAT=check;不勾选 = /QT_WECHAT=skip)');

  // §2.9.3 末 MULTIPLE_INSTALLS:列出各处路径/版本/是否正在运行,用户选**一处**;
  // 其余**不卸、不改**。只有真检测到 ≥2 处时才显示(ShouldSkipPage 控制)。
  PageWeChatSelect := CreateInputOptionPage(PageWeChat.ID,
    '选择要使用的微信', '本机检测到多处微信安装',
    '请选一处作为 QTrade 使用的微信。**其余各处我们一律不动**(不卸载、不修改)。' + #13#10 +
    '选中的那一处会按版本矩阵重新判定,必要时引导你换成随包的 4.1.12.26。',
    True, False);   // Exclusive = True(单选)
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = PageKernelConfirm.ID then
    PageKernelConfirm.SubCaptionLabel.Caption :=
      '接下来要把 WSL 切换到 QTrade 的内核。这一步**会关闭 WSL 里正在运行的所有程序**' +
      '(你的 Ubuntu 终端、Docker Desktop 里的容器、正在编辑的文件等),相当于给 WSL 重启一次。' +
      '切换后会自动验证,验证不通过会**自动恢复原来的内核**。' + #13#10#13#10 +
      '当前检测到正在运行的发行版:' + GetRunningDistros();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Count, I: Integer;
begin
  Result := True;
  if CurPageID = PageKernelConfirm.ID then
  begin
    // 勾选「我已知晓内核替换的影响」才能下一步(§4 欢迎与告知行)
    if not PageKernelConfirm.Values[0] then
    begin
      MsgBox('请先勾选「我已知晓内核替换的影响」。', mbInformation, MB_OK);
      Result := False;
      Exit;
    end;
    AckKernelImpact := True;
    OptAcceptShutdown := True;   // 交互模式下,用户在本页的勾选 = 红线 6 的明示确认
  end;
  if CurPageID = PageWeChat.ID then
  begin
    if not PageWeChat.Values[0] then
    begin
      OptWeChat := 'skip';
      WeChatMultiple := False;
    end
    else
    begin
      WeChatUserApproved := True;
      // 只读检测(不动微信),为 MULTIPLE_INSTALLS 的选择页备料
      if RunStep('wechat-detect') then
      begin
        Count := JsonInt(LastStepJson, 'count', 0);
        WeChatMultiple := Count >= 2;
        if WeChatMultiple then
        begin
          SetArrayLength(WeChatInstallPaths, Count);
          for I := 0 to Count - 1 do
          begin
            WeChatInstallPaths[I] := JsonStr(LastStepJson, 'install_' + IntToStr(I) + '_path');
            PageWeChatSelect.Add(JsonStr(LastStepJson, 'install_' + IntToStr(I) + '_label'));
          end;
          PageWeChatSelect.SelectedValueIndex := 0;
        end;
      end;
    end;
  end;
  if CurPageID = PageWeChatSelect.ID then
  begin
    if PageWeChatSelect.SelectedValueIndex >= 0 then
      WeChatSelectedPath := WeChatInstallPaths[PageWeChatSelect.SelectedValueIndex];
  end;
end;

// ── 启动:解析命令行 + bootstrap(互斥体、残留 kcheck、续跑点)───────────────
function InitializeSetup(): Boolean;
var
  Handle: Cardinal;
begin
  ParseCommandLine();
  // 并发保护(§2.12):互斥体由本进程持有到退出为止
  Handle := CreateMutexW(0, False, 'Global\QTradeSetup');
  if (Handle = 0) or (GetLastError() = ERROR_ALREADY_EXISTS) then
  begin
    if not IsSilentRun() then
      MsgBox('已有一个 QTrade 安装程序正在运行。', mbInformation, MB_OK);
    ExitProcess(E_INSTALL_ALREADY_RUNNING);
  end;
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  StateName: String;
  Answer: Integer;
begin
  if CurStep <> ssInstall then Exit;

  // 0) 引擎自检:`Global\QTradeSetup` 互斥体、清残留 kcheck、决定从哪步续跑(§2.12)
  if not RunStep('bootstrap') then
    FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));

  if CompareText(OptMode, 'uninstall') = 0 then
  begin
    if not RunStep('uninstall') then
      FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
    ExitProcess(OK);
  end;

  // §2.13:新版本 EXE 检测到 `install_state.state=DONE` 且 `package_version <` 自身即进 upgrade;
  // bootstrap 已算好建议模式,这里只在用户没显式给 /QT_MODE 时采纳它。
  if (CompareText(OptMode, 'install') = 0) then
  begin
    StateName := JsonStr(LastStepJson, 'recommended_mode');
    if CompareText(StateName, 'upgrade') = 0 then OptMode := 'upgrade';
    if CompareText(StateName, 'downgrade-refused') = 0 then
      FailWith(E_INSTALL_DOWNGRADE_REFUSED,
        '本机已安装的版本高于本安装包,不支持降级。请先卸载现有版本,再安装这一版。');
  end;

  if CompareText(OptMode, 'upgrade') = 0 then
  begin
    // schema_breaking 时向导第一页明示 + 二次确认(A-5;验收 M1-22b)
    if not RunStep('upgrade') then
    begin
      if JsonStr(LastStepJson, 'reason') = 'WAIT_SCHEMA_BREAKING_CONFIRM' then
      begin
        if IsSilentRun() then FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
        Answer := MsgBox(JsonStr(LastStepJson, 'message') + #13#10#13#10 + '确定要继续吗?', mbConfirmation, MB_YESNO);
        if Answer <> IDYES then ExitProcess(OK);
        AcceptSchemaBreaking := True;
        if MsgBox('再确认一次:本次升级会重建数据。升级前会自动做一份备份。确定继续?', mbConfirmation, MB_YESNO) <> IDYES then
          ExitProcess(OK);
        if not RunStep('upgrade') then FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
      end
      else if JsonStr(LastStepJson, 'reason') = 'WAIT_SHUTDOWN_CONFIRM' then
      begin
        if IsSilentRun() then FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
        // 🔴 [NOSHUTDOWN] / 红线 6:停账号容器会中断账号,先明示、用户确认时机
        if MsgBox(JsonStr(LastStepJson, 'message') + #13#10#13#10 + '现在开始升级吗?', mbConfirmation, MB_YESNO) <> IDYES then
          ExitProcess(E_INSTALL_WAIT_USER);
        OptAcceptShutdown := True;
        if not RunStep('upgrade') then FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
      end
      else
        FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
    end;
    ExitProcess(OK);
  end;

  if CompareText(OptMode, 'repair') = 0 then
  begin
    if not RunStep('repair') then
      FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
    ExitProcess(OK);
  end;

  // 1) PRECHECK(§2.4)——只读;失败即停,机上除 logs\ 外无落盘
  WizardForm.StatusLabel.Caption := '正在检查运行环境…';
  if not RunStep(ST_PRECHECK) then
    FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));

  // 2) PAYLOAD_STAGED(§2.2.1)——外壳已解压,这里逐文件复核 sha256
  WizardForm.StatusLabel.Caption := '正在校验安装包…';
  if not RunStep(ST_PAYLOAD_STAGED) then
    FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));

  // 3) WSL_FEATURE(§2.5.1)——需要重启则写 RunOnce 并退 3010,**不自动重启**
  WizardForm.StatusLabel.Caption := '正在启用 Windows 组件…';
  if not RunStep(ST_WSL_FEATURE) then
    FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
  StateName := JsonStr(LastStepJson, 'state');
  if CompareText(StateName, ST_REBOOT_PENDING) = 0 then
  begin
    if OptAcceptReboot then
    begin
      Exec(ExpandConstant('{sys}\shutdown.exe'), '/r /t 15', '', SW_HIDE, ewNoWait, LastStepExit);
      ExitProcess(E_INSTALL_REBOOT_REQUIRED);
    end;
    if not IsSilentRun() then
      MsgBox('Windows 组件已启用,需要重启一次。重启并登录后安装会自动继续(会弹出一次权限确认)。',
             mbInformation, MB_OK);
    ExitProcess(E_INSTALL_REBOOT_REQUIRED);
  end;

  // 4) WSL_MSI(§2.5.2)——只升不降;不调 `wsl --update`(零联网)
  WizardForm.StatusLabel.Caption := '正在安装 WSL 运行时…';
  if not RunStep(ST_WSL_MSI) then
    FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));

  // 5) KERNEL_STAGED(§2.6.1)——落盘 + ACL + 指针 + 在**官方内核上**预导入 kcheck
  WizardForm.StatusLabel.Caption := '正在准备 QTrade 内核…';
  if not RunStep(ST_KERNEL_STAGED) then
    FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));

  // 6) 内核原子段(§2.6.3):写配置 → shutdown → 验证 →(必要时)自动回滚
  //    🔴 红线 6:静默模式必须带 /QT_ACCEPT_SHUTDOWN=1,否则停车退 10
  WizardForm.StatusLabel.Caption := '正在切换 WSL 内核并验证…';
  if not RunStep('KERNEL_SWITCH') then
    FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));

  // 7) DISTRO_IMPORTED(§2.7)——含 §2.7.1 同名冲突的用户选择(**不改名绕过**)
  WizardForm.StatusLabel.Caption := '正在导入发行版(约 3–6 分钟)…';
  if not RunStep(ST_DISTRO_IMPORTED) then
  begin
    if JsonStr(LastStepJson, 'reason') = 'WAIT_DISTRO_CONFLICT' then
    begin
      if IsSilentRun() then
        FailWith(E_INSTALL_DISTRO_NAME_CONFLICT_DECLINED,
          '发现不是本程序创建的同名发行版 qtrade;静默模式下不做处置。请先手工处理后重试。');
      Answer := MsgBox(JsonStr(LastStepJson, 'message') + #13#10#13#10 +
        '【是】= 先导出备份再注销它,然后重新导入(导出失败就不会注销);' + #13#10 +
        '【否】= 取消安装。' + #13#10#13#10 +
        '注意:不提供「改名绕过」—— 端口、路径、WinAgent 都写死 qtrade,改名等于改产品。',
        mbConfirmation, MB_YESNO);
      if Answer = IDYES then DistroConflictChoice := 'unregister' else DistroConflictChoice := 'cancel';
      if not RunStep(ST_DISTRO_IMPORTED) then
        FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
    end
    else
      FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
  end;

  // 8) IMAGES_LOADED(§2.7.3 末 / §2.2.1 G-10)
  WizardForm.StatusLabel.Caption := '正在加载镜像并等待 Agent 就绪…';
  if not RunStep(ST_IMAGES_LOADED) then
  begin
    // §2.7.3:docker 起不来且 kcheck 曾通过 ⇒ 给【回滚内核】(安装期由引擎直接执行 §2.6.5)
    if (JsonStr(LastStepJson, 'reason') = 'DOCKER_NOT_READY') and (not IsSilentRun())
       and JsonBool(LastStepJson, 'after_kernel_switch') then
    begin
      Answer := MsgBox('docker 在新内核下没有就绪。' + #13#10#13#10 +
        '要现在回滚到原来的内核吗?回滚后 QTrade 将无法使用,但你原有的 WSL 环境会恢复。',
        mbConfirmation, MB_YESNO);
      if Answer = IDYES then RunStep('verify-kernel');
    end;
    FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
  end;

  // 9) WINAGENT_INSTALLED(§2.8.3)
  WizardForm.StatusLabel.Caption := '正在安装 WinAgent…';
  if not RunStep(ST_WINAGENT_INSTALLED) then
    FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));

  // 10) CLIENTS_CHECKED(§2.9)——微信重装引导(红线 8:必须引导、用户确认后才动)
  WizardForm.StatusLabel.Caption := '正在检查客户端…';
  if (CompareText(OptWeChat, 'skip') <> 0) and (not IsSilentRun()) then
  begin
    Answer := MsgBox(
      '要启用微信通道,需要把微信换成随包的 4.1.12.26 版本。' + #13#10#13#10 +
      '我们会**直接覆盖安装**(不卸载)。你的聊天记录与登录态在微信数据目录里,覆盖安装不会碰它们;' +
      '可以先做一份备份作为保险。换版本后微信会关闭自动更新,可能需要重新扫码登录一次。' + #13#10#13#10 +
      '【是】= 备份并覆盖安装;【否】= 跳过(不启用微信通道)。',
      mbConfirmation, MB_YESNO);
    if Answer = IDYES then
    begin
      WeChatUserApproved := True;
      WeChatBackupModeSel := 'auto';
    end
    else
      OptWeChat := 'skip';
  end;
  if not RunStep(ST_CLIENTS_CHECKED) then
  begin
    // §2.9.3 方案②:方案①失败 / 3.x 升 4.x / 用户要清掉一处 ⇒ 交互式卸载再装。
    // 🔴 卸载器**不加任何参数**(`/S` = 卸载并清空聊天记录与登录态);
    //    超时也不由我们替用户决定,按 §2.9.3 给【我已卸载,继续】/【跳过微信通道】。
    if (JsonStr(LastStepJson, 'reason') = 'WAIT_WECHAT_UNINSTALL') and (not IsSilentRun()) then
    begin
      if JsonBool(LastStepJson, 'timed_out') then
        Answer := MsgBox('等待微信卸载超时。' + #13#10#13#10 +
          '【是】= 我已经卸载完了,继续装随包的 4.1.12.26;' + #13#10 +
          '【否】= 跳过微信通道(安装继续,微信模块保持关闭)。', mbConfirmation, MB_YESNO)
      else
        Answer := MsgBox(JsonStr(LastStepJson, 'message') + #13#10#13#10 +
          '接下来会**以交互方式**打开微信自己的卸载程序(不带任何静默参数)。' + #13#10 +
          '🔴 请务必确认卸载器里的「保留本地数据」勾选框是**勾上的** —— 取消勾选会删掉聊天记录、备份与设置。' + #13#10#13#10 +
          '【是】= 现在卸载并重装;【否】= 跳过微信通道。', mbConfirmation, MB_YESNO);
      if Answer = IDYES then
      begin
        WeChatUninstallConfirmed := True;
        if not RunStep(ST_CLIENTS_CHECKED) then
          FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
      end
      else
      begin
        OptWeChat := 'skip';
        if not RunStep(ST_CLIENTS_CHECKED) then
          FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
      end;
    end
    else
      FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));
  end;

  // 11) SELFTEST_OK(§2.11)
  WizardForm.StatusLabel.Caption := '正在自检(启动一个临时安卓实例)…';
  if not RunStep(ST_SELFTEST_OK) then
    FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));

  // 12) DONE(§2.11 第 5 项:清 RunOnce、快捷方式、删暂存 rootfs.tar、写摘要)
  WizardForm.StatusLabel.Caption := '正在收尾…';
  if not RunStep(ST_DONE) then
    FailWith(LastStepExit, JsonStr(LastStepJson, 'message'));

  WizardForm.StatusLabel.Caption := '安装完成';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  // 静默模式下不显示任何自定义页(确认改由 /QT_ACCEPT_SHUTDOWN=1 给出,B-7)
  if IsSilentRun() then
  begin
    Result := True;
    Exit;
  end;
  // 只在真检测到 ≥2 处微信安装时才显示选择页(§2.9.3 末)
  if PageID = PageWeChatSelect.ID then Result := not WeChatMultiple;
end;

procedure DeinitializeSetup();
begin
  // 正常路径下把最后一步的退出码原样透传给 SFX 外壳(§2.1)
  if LastStepExit <> 0 then ExitProcess(LastStepExit);
end;
