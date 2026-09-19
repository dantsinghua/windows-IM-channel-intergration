import sys, lzma, struct, re
data=open(sys.argv[1],'rb').read()
sig=b'\xEF\xBE\xAD\xDENullsoftInst'
i=data.find(sig)
if i<0: sys.exit("no NSIS sig")
fh=i-4
flags,=struct.unpack('<I',data[fh:fh+4])
hdr_len,arc_len=struct.unpack('<II',data[fh+20:fh+28])
print(f"firstheader@{fh} flags={flags:#x} header_len={hdr_len} archive_len={arc_len}")
p=fh+28
blk,=struct.unpack('<I',data[p:p+4]); comp=blk>>31; ln=blk&0x7fffffff
print(f"header block: compressed={comp} len={ln}")
raw=data[p+4:p+4+ln]
props=raw[:5]; body=raw[5:]
dec=lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
try:
    out=dec.decompress(props+b'\xff'*8+body, max_length=hdr_len)
except Exception as e:
    print("decompress err",e); out=b''
print("decompressed",len(out),"bytes (expected",hdr_len,")")
open(sys.argv[2],'wb').write(out)
