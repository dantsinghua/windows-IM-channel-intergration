import json
from qtrade_agent.ossbox.sts import StsCredentialProvider
from qtrade_agent.ossbox.oss_http import OssV1Box
from test_ossbox_sts import FakeOpener,FakeResponse,FakeClock,_credentials,_STS_URL,_AUTH_KEY,_SM4_KEY,_headers

def test_no_bucket_sts_response_uses_configured_bucket_for_oss_get_put():
    data=_credentials()
    data.pop("bucket")
    sts=FakeOpener(FakeResponse(data))
    provider=StsCredentialProvider(_STS_URL,_AUTH_KEY,_SM4_KEY,opener=sts,clock=FakeClock())
    box=OssV1Box(endpoint="oss-cn-hangzhou.aliyuncs.com",bucket="qtrade-nyoss-lab",credentials_provider=provider)
    oss=FakeOpener(FakeResponse(b"synthetic-object"),FakeResponse(b"ok"))
    box._opener=oss
    assert box.get("synthetic/test.bin")==b"synthetic-object"
    assert box.put("synthetic/test.bin",b"payload") is None
    assert len(oss.requests)==2
    for request in oss.requests:
        assert request.full_url=="https://qtrade-nyoss-lab.oss-cn-hangzhou.aliyuncs.com/synthetic/test.bin"
        header=_headers(request)
        assert header["x-oss-security-token"]==data["security_token"]
        assert header["authorization"].startswith("OSS STS.")
