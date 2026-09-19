import struct, sys
b=open(sys.argv[1],'rb').read()
u=lambda o: struct.unpack_from('<i',b,o)[0]
blocks=[(u(4+8*i),u(8+8*i)) for i in range(8)]
names=['pages','sections','entries','strings','langtables','ctlcolors','bgfont','data']
for n,(o,c) in zip(names,blocks): print(f"block {n}: off={o} num={c}")
so,sc=blocks[3]; eo,ec=blocks[2]
strblk=b[so:blocks[4][0]]
VARS=[f"${i}" for i in range(10)]+[f"$R{i}" for i in range(10)]+["$CMDLINE","$INSTDIR","$OUTDIR","$EXEDIR","$LANGUAGE","$TEMP","$PLUGINSDIR","$EXEPATH","$EXEFILE","$HWNDPARENT","$_CLICK","$__SHELL"]
SHELL={0x1a:"$APPDATA",0x24:"$WINDIR",0x25:"$SYSDIR",0x26:"$PROGRAMFILES",0x00:"$DESKTOP",0x10:"$DESKTOP",0x05:"$DOCUMENTS",0x2b:"$COMMONFILES",0x1c:"$LOCALAPPDATA",0x23:"$APPDATA(common)",0x07:"$SMSTARTUP",0x0b:"$SMPROGRAMS",0x02:"$SMPROGRAMS",0x08:"$RECENT"}
def s(idx):
    if idx<0: return "?"
    off=idx*2; out=""
    while off+1<len(strblk):
        c=struct.unpack_from('<H',strblk,off)[0]; off+=2
        if c==0: break
        if 0x8080<=c<=0x80FF:
            x=c&0x7f; out+= VARS[x] if x<len(VARS) else f"$VAR{x}"; continue
        if 0x1000<=c<=0x2FFF:
            out+= SHELL.get(c&0xff, f"$SHELL{c&0xff:#x}"); continue
        if c in (0xE000,0xE001,0xE002,0xE003):
            a=struct.unpack_from('<H',strblk,off)[0]; off+=2; x=a&0x7fff
            if c==0xE001:
                out+= VARS[x] if x<len(VARS) else f"$VAR{x}"
            elif c==0xE002:
                out+= SHELL.get(x&0xff, f"$SHELL{x&0xff:#x}")+("" if (x>>8)==0 else f"/all{x>>8:#x}")
            elif c==0xE003:
                out+= f"$LANGSTR{x}"
            else: out+=chr(a)
        else: out+=chr(c)
    return out
OPS={1:"Return",2:"Nop",3:"Abort",4:"Quit",5:"Call",6:"DetailPrint",7:"Sleep",8:"BringToFront",9:"SetDetailsView",10:"SetFileAttributes",11:"CreateDirectory",12:"IfFileExists",13:"SetFlag",14:"IfFlag",15:"GetFlag",16:"Rename",17:"GetFullPathName",18:"SearchPath",19:"GetTempFileName",20:"File",21:"Delete",22:"MessageBox",23:"RMDir",24:"StrLen",25:"StrCpy",26:"StrCmp",27:"ReadEnvStr",28:"IntCmp",29:"IntOp",30:"IntFmt",31:"Push/Pop/Exch",32:"FindWindow",33:"SendMessage",34:"IsWindow",35:"GetDlgItem",36:"SetCtlColors",37:"SetBrandingImage",38:"CreateFont",39:"ShowWindow",40:"ExecShell",41:"Exec/ExecWait",42:"GetFileTime",43:"GetDLLVersion",44:"Plugin/RegDLL",45:"CreateShortCut",46:"CopyFiles",47:"Reboot",48:"WriteINIStr",49:"ReadINIStr",50:"DeleteReg",51:"WriteReg",52:"ReadRegStr",53:"EnumReg",54:"FileClose",55:"FileOpen",56:"FileWrite",57:"FileRead",58:"FileSeek",59:"FindClose",60:"FindNext",61:"FindFirst",62:"WriteUninstaller",63:"Log",64:"SectionSet",65:"InstTypeSet",66:"GetOSInfo",67:"GetLabelAddr",68:"GetFunctionAddr",69:"LockWindow",70:"FileWriteUTF16",71:"FileReadUTF16",72:"Log2",73:"FindProcDLL?"}
FLAGS={0:"autoclose",1:"all_user_var",2:"errors",3:"abort",4:"reboot",5:"reboot_called",6:"cur_insttype",7:"plugin_api",8:"SILENT",9:"instdir_error",10:"rtl",11:"errlvl",12:"alter_reg_view",13:"status_update"}
def show(i):
    o=eo+i*28; w=u(o); a=[u(o+4+4*k) for k in range(6)]
    n=OPS.get(w,f"op{w}")
    if w==14: d=f"IfFlag {FLAGS.get(a[1],a[1])} -> jump {a[0]} else fall"
    elif w==13: d=f"SetFlag {FLAGS.get(a[0],a[0])} = {s(a[1])}"
    elif w==44: d=f"Plugin {s(a[0])} :: {s(a[1])}  (nounload={a[3]})"
    elif w==31: d=("Exch" if a[2] else ("Pop "+s(a[0]) if a[1] else "Push "+s(a[0]))) if True else ""
    elif w in (21,23,11,6,20,63): d=f"{n} {s(a[0])} (flags {a[1]})"
    elif w==22: d=f"MessageBox {s(a[0])} flags={a[1]:#x} ret1={a[3]}->jmp{a[4]} ret2={a[5]}"
    elif w==25: d=f"StrCpy {VARS[a[0]] if a[0]<len(VARS) else '$VAR'+str(a[0])} = {s(a[1])} [{s(a[2])},{s(a[3])}]"
    elif w==26: d=f"StrCmp {s(a[0])} == {s(a[1])} ? jmp{a[2]} : jmp{a[3]}"
    elif w==28: d=f"IntCmp {s(a[0])} vs {s(a[1])} eq{a[2]} lt{a[3]} gt{a[4]}"
    elif w==12: d=f"IfFileExists {s(a[0])} yes{a[1]} no{a[2]}"
    elif w==5: d=f"Call entry {a[0]}"
    elif w in (40,41): d=f"{n} {s(a[0])} {s(a[1])} {s(a[2])}"
    elif w==49: d=f"ReadINIStr var{a[0]} <- [{s(a[1])}]{s(a[2])} in {s(a[3])}"
    elif w==48: d=f"WriteINIStr [{s(a[0])}]{s(a[1])}={s(a[2])} in {s(a[3])}"
    elif w in (50,51,52): d=f"{n} root{a[0]} {s(a[1])} \\ {s(a[2])} = {s(a[3]) if w==51 else ''}"
    elif w==2 and a[0]: d=f"Goto {a[0]}"
    elif w==33: d=f"SendMessage {s(a[1])},{s(a[2])},{s(a[3])},{s(a[4])}"
    elif w==32: d=f"FindWindow {s(a[1])} {s(a[2])}"
    elif w==29: d=f"IntOp var{a[0]} = {s(a[1])} op{a[3]} {s(a[2])}"
    else: d=f"{n} {a}"
    return f"{i:4d}: {d}"
hdr_cb=["onInit","onInstSuccess","onInstFailed","onUserAbort","onGUIInit","onGUIEnd","onMouseOverSection","onVerifyInstDir","onSelChange","onRebootFailed"]
for k,nm in enumerate(hdr_cb): print(f"callback {nm} -> entry {u(108+4*k)}")
po,pc=blocks[0]
for i in range(pc):
    o=po+i*68; f=[u(o+4*k) for k in range(17)]
    print(f"page{i}: id={f[0]} wndproc={f[1]} dlg={f[2]} pre={f[3]} show={f[4]} leave={f[5]} flags={f[6]:#x} caption={s(f[7])!r}")
so2,sc2=blocks[1]
for i in range(sc2):
    o=so2+i*(1024*2+24) if False else so2
    f=[u(o+4*k) for k in range(6)]
    print(f"section{i}: name={s(f[0])!r} insttypes={f[1]} flags={f[2]:#x} code_entry={f[3]} code_size={f[4]} size_kb={f[5]}")
for i in range(ec): print(show(i))
print("--- pages ---")
po,pc=blocks[0]
for i in range(pc):
    o=po+i*0x44 if False else po+i*68  # page struct size?
