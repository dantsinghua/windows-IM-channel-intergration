// Compile the actual staging class with a deterministic in-memory Win32 model.
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <cwchar>
#include <map>
#include <memory>
#include <string>
#include <vector>

using DWORD = std::uint32_t;
using WCHAR = wchar_t;
using PSID = void *;
using SECURITY_DESCRIPTOR_CONTROL = unsigned short;
constexpr unsigned MAX_PATH = 260;
constexpr DWORD ERROR_SUCCESS=0, ERROR_ALREADY_EXISTS=183;
constexpr DWORD READ_CONTROL=0x20000, FILE_READ_ATTRIBUTES=0x80, FILE_SHARE_READ=1;
constexpr DWORD OPEN_EXISTING=3, FILE_FLAG_BACKUP_SEMANTICS=0x02000000, FILE_FLAG_OPEN_REPARSE_POINT=0x00200000;
constexpr DWORD FILE_ATTRIBUTE_DIRECTORY=0x10, FILE_ATTRIBUTE_REPARSE_POINT=0x400;
constexpr DWORD OWNER_SECURITY_INFORMATION=1, DACL_SECURITY_INFORMATION=4, SE_DACL_PROTECTED=0x1000;
constexpr DWORD WRITE_DAC=0x40000, WRITE_OWNER=0x80000, DELETE=0x10000, FILE_DELETE_CHILD=0x40;
constexpr DWORD GENERIC_ALL=0x10000000, GENERIC_WRITE=0x40000000;
constexpr DWORD FILE_WRITE_DATA=2, FILE_APPEND_DATA=4, FILE_WRITE_EA=0x10, FILE_WRITE_ATTRIBUTES=0x100;
constexpr DWORD INHERIT_ONLY_ACE=8, ACCESS_DENIED_ACE_TYPE=1, ACCESS_ALLOWED_ACE_TYPE=0;
constexpr DWORD SE_FILE_OBJECT=1, SDDL_REVISION_1=1, CSIDL_COMMON_APPDATA=35, SHGFP_TYPE_CURRENT=0;
constexpr int S_OK=0, FALSE=0;
#define Z7_ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))

struct ACE_HEADER { unsigned char AceType, AceFlags; unsigned short AceSize; };
struct ACCESS_ALLOWED_ACE { ACE_HEADER Header; DWORD Mask; DWORD SidStart; };
struct ACL {
  DWORD AceCount=2;
  std::vector<ACCESS_ALLOWED_ACE> aces={{{0,0,0},GENERIC_ALL,1},{{0,0,0},GENERIC_ALL,2}};
};
using PACL=ACL *;
struct Descriptor { ACL *acl; SECURITY_DESCRIPTOR_CONTROL control; };
using PSECURITY_DESCRIPTOR=Descriptor *;
struct SECURITY_ATTRIBUTES { DWORD nLength; PSECURITY_DESCRIPTOR lpSecurityDescriptor; int bInheritHandle; };
struct BY_HANDLE_FILE_INFORMATION { DWORD dwFileAttributes; };
struct GUID { unsigned serial; };
struct Node { DWORD owner=2, attrs=FILE_ATTRIBUTE_DIRECTORY; ACL acl; bool protectedDacl=true; };
struct Handle { Node *node; bool closed=false; };
using HANDLE=Handle *;
#define INVALID_HANDLE_VALUE reinterpret_cast<HANDLE>(-1)

class UString {
  std::wstring value;
public:
  UString()=default;
  UString(const wchar_t *s):value(s) {}
  UString(std::wstring s):value(std::move(s)) {}
  const wchar_t *Ptr() const { return value.c_str(); }
  unsigned Len() const { return value.size(); }
  wchar_t Back() const { return value.back(); }
  void DeleteBack() { value.pop_back(); }
  void Empty() { value.clear(); }
  bool IsEmpty() const { return value.empty(); }
  UString Left(unsigned n) const { return value.substr(0,n); }
  UString Mid(unsigned pos,unsigned n) const { return value.substr(pos,n); }
  UString &operator+=(const wchar_t *s) { value+=s; return *this; }
  UString &operator+=(wchar_t c) { value+=c; return *this; }
  UString &operator+=(const UString &s) { value+=s.value; return *this; }
};
namespace NName { bool IsDrivePath(const wchar_t *s) {
  return ((s[0]>=L'A' && s[0]<=L'Z') || (s[0]>=L'a' && s[0]<=L'z')) && s[1]==L':' && s[2]==L'\\';
} }

static std::map<std::wstring,Node> nodes;
static std::vector<std::unique_ptr<Handle>> handles;
static std::vector<std::wstring> created;
static std::wstring programData=L"E:\\Corporate Data";
static DWORD lastError=0;
static unsigned nextGuid=0;
static bool guidFails=false;
static std::wstring refusedPath;
static bool aclQueryFails=false;

bool EqualSid(PSID a,PSID b) { return a && b && *static_cast<DWORD*>(a)==*static_cast<DWORD*>(b); }
bool IsValidSid(PSID a) { return a && *static_cast<DWORD*>(a)>=1 && *static_cast<DWORD*>(a)<=9; }
bool IsValidAcl(PACL a) { return a && a->AceCount==a->aces.size(); }
void *LocalFree(void *p) { std::free(p); return nullptr; }
bool ConvertStringSidToSidW(const wchar_t *text,PSID *out) {
  auto p=static_cast<DWORD*>(std::malloc(sizeof(DWORD)));
  *p=std::wstring(text)==L"S-1-5-18"?1:std::wstring(text)==L"S-1-5-32-544"?2:3;
  *out=p; return true;
}
bool ConvertStringSecurityDescriptorToSecurityDescriptorW(const wchar_t *text,DWORD,PSECURITY_DESCRIPTOR *out,void*) {
  assert(std::wstring(text)==L"O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)");
  *out=static_cast<Descriptor*>(std::malloc(sizeof(Descriptor)));
  **out={nullptr,SE_DACL_PROTECTED}; return true;
}
DWORD GetSecurityInfo(HANDLE h,DWORD,DWORD,PSID *owner,void*,PACL *acl,void*,PSECURITY_DESCRIPTOR *out) {
  if(aclQueryFails) return 5;
  *owner=&h->node->owner; *acl=&h->node->acl;
  *out=static_cast<Descriptor*>(std::malloc(sizeof(Descriptor)));
  **out={*acl,static_cast<SECURITY_DESCRIPTOR_CONTROL>(h->node->protectedDacl?SE_DACL_PROTECTED:0)};
  return 0;
}
bool GetSecurityDescriptorControl(PSECURITY_DESCRIPTOR sd,SECURITY_DESCRIPTOR_CONTROL *control,DWORD *rev) {
  *control=sd->control; *rev=1; return true;
}
bool GetAce(PACL a,DWORD i,void **out) { if(i>=a->aces.size()) return false; *out=&a->aces[i]; return true; }
HANDLE CreateFileW(const wchar_t *path,DWORD access,DWORD sharing,void*,DWORD creation,DWORD flags,void*) {
  assert(access==(READ_CONTROL|FILE_READ_ATTRIBUTES)); assert(sharing==FILE_SHARE_READ);
  assert(creation==OPEN_EXISTING); assert(flags==(FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT));
  auto it=nodes.find(path); if(it==nodes.end() || path==refusedPath) return INVALID_HANDLE_VALUE;
  handles.emplace_back(new Handle{&it->second}); return handles.back().get();
}
bool GetFileInformationByHandle(HANDLE h,BY_HANDLE_FILE_INFORMATION *out) { out->dwFileAttributes=h->node->attrs; return true; }
bool CloseHandle(HANDLE h) { assert(h && !h->closed); h->closed=true; return true; }
bool CreateDirectoryW(const wchar_t *path,SECURITY_ATTRIBUTES *attrs) {
  assert(attrs && attrs->lpSecurityDescriptor && !attrs->bInheritHandle);
  if(nodes.count(path)) { lastError=ERROR_ALREADY_EXISTS; return false; }
  nodes.emplace(path,Node{}); created.emplace_back(path); return true;
}
DWORD GetLastError() { return lastError; }
int SHGetFolderPathW(void*,DWORD,void*,DWORD,wchar_t *out) { std::wcscpy(out,programData.c_str()); return S_OK; }
int CoCreateGuid(GUID *id) { if(guidFails) return 1; id->serial=++nextGuid; return S_OK; }
int StringFromGUID2(GUID id,wchar_t *text,unsigned len) {
  return std::swprintf(text,len,L"{00000000-0000-0000-0000-%012u}",id.serial)+1;
}

#include "QTradeStage.h"

static std::wstring makeName(unsigned n) {
  wchar_t out[40]; StringFromGUID2(GUID{n},out,40);
  return programData+L"\\QTrade-Staging\\"+std::wstring(out+1,36);
}
static void reset() {
  nodes.clear(); handles.clear(); created.clear(); nextGuid=0; guidFails=false;
  refusedPath.clear(); aclQueryFails=false;
  nodes[L"E:\\"]=Node{}; nodes[programData]=Node{};
}
static void closed() { for(const auto &h:handles) assert(h->closed); }
int main(int argc,char **argv) {
  assert(argc==2); const std::string scenario=argv[1]; reset();
  const std::wstring base=programData+L"\\QTrade-Staging";
  if(scenario=="unique") {
    UString a,b;
    { CQTradeStage first; assert(first.Create(L"%ProgramData%\\QTrade-Staging",a));
      CQTradeStage second; assert(second.Create(L"%ProgramData%\\QTrade-Staging",b));
      assert(std::wstring(a.Ptr())!=b.Ptr()); assert(std::wstring(a.Ptr()).find(base+L"\\")==0);
      for(const auto &h:handles) assert(!h->closed); }
    assert(!nodes.count(programData+L"\\QTrade")); closed(); return 0;
  }
  if(scenario=="collision") { nodes[base]=Node{}; nodes[makeName(1)]=Node{}; }
  if(scenario=="all-collisions") { nodes[base]=Node{}; for(unsigned i=1;i<=8;i++) nodes[makeName(i)]=Node{}; }
  if(scenario=="handle-failure") refusedPath=base;
  if(scenario=="acl-query-failure") aclQueryFails=true;
  if(scenario=="junction-base") { nodes[base]=Node{}; nodes[base].attrs|=FILE_ATTRIBUTE_REPARSE_POINT; }
  if(scenario=="junction-parent") nodes[programData].attrs|=FILE_ATTRIBUTE_REPARSE_POINT;
  if(scenario=="unsafe-base") { nodes[base]=Node{}; nodes[base].acl.aces.push_back({{0,0,0},FILE_WRITE_DATA,9}); nodes[base].acl.AceCount++; }
  if(scenario=="unprotected-base") { nodes[base]=Node{}; nodes[base].protectedDacl=false; }
  if(scenario=="unsafe-ancestor") { nodes[programData].acl.aces.push_back({{0,0,0},DELETE,9}); nodes[programData].acl.AceCount++; }
  if(scenario=="guid-failure") guidFails=true;
  UString destination;
  { CQTradeStage stage;
    const bool ok=stage.Create(scenario=="live-path"?L"%ProgramData%\\QTrade":L"%ProgramData%\\QTrade-Staging",destination);
    assert(ok==(scenario=="collision"));
    if(ok) assert(std::wstring(destination.Ptr())==makeName(2));
    else assert(destination.IsEmpty());
  }
  assert(!nodes.count(programData+L"\\QTrade")); closed();
  return 0;
}
