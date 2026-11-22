import hashlib, os, struct, time
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pms.tgproxy import (pms_tg_parse_secret, pms_tg_build_obfuscated_header,
                         pms_tg_build_req_pq_frame, _pms_tg_parse_abridged_frame)
from pms.dcprobe import _PMS_MTPROTO_REQ_PQ_MULTI, _PMS_MTPROTO_RES_PQ, _PMS_MTPROTO_MSG_ID_SHIFT

def ctr(key, iv): return Cipher(algorithms.AES(key), modes.CTR(iv))

def sim_proxy(secret_raw, F):
    rev = F[55:7:-1]
    pdec_key = hashlib.sha256(F[8:40] + secret_raw).digest(); pdec_iv = F[40:56]
    penc_key = hashlib.sha256(rev[:32] + secret_raw).digest(); penc_iv = rev[32:48]
    return ctr(pdec_key, pdec_iv).decryptor(), ctr(penc_key, penc_iv).encryptor()

ok_all = True
for name, sec in [('dd','3XnnAQIAAQAH8AMDhuJMOt0'),
                  ('ee+domain','eefdb3d4e176736e98173509e71d35e6827061706f6d6567612e776869736b657974616e676f666f7874726f742e6c6f6c'),
                  ('ee','ee8704bc882dc9454091bf7b21ea16e76c647a656e2e7275')]:
    s = pms_tg_parse_secret(sec)
    assert s.ok, s.error
    tag = b'\xee\xee\xee\xee' if s.form=='dd' else b'\xef'
    F, enc, dec = pms_tg_build_obfuscated_header(s.raw, 2, tag)
    assert len(F) == 64
    pdec, penc = sim_proxy(s.raw, F)
    # 代理消费首包对齐 CTR
    pdec.update(F)
    # 客户端发 req_pq
    frame, nonce = pms_tg_build_req_pq_frame(enc)
    pf = pdec.update(frame)
    payload, used = _pms_tg_parse_abridged_frame(pf)
    assert payload is not None and struct.unpack('<I', payload[8:12])[0] == _PMS_MTPROTO_REQ_PQ_MULTI, f'{name}: proxy cannot parse req_pq'
    assert payload[12:28] == nonce, f'{name}: nonce mismatch'
    # 代理回 resPQ（echo nonce）
    rnonce = os.urandom(16)
    msg_id = (int(time.time()*1000) << _PMS_MTPROTO_MSG_ID_SHIFT) | (os.urandom(1)[0] & 0b111); msg_id &= ~0b11
    body = struct.pack('<QI', msg_id, _PMS_MTPROTO_RES_PQ) + nonce + rnonce
    w = len(body)//4
    rf = (bytes([w]) + body) if w < 128 else (bytes([0]) + struct.pack('<I', w)[:3] + body)
    cf = penc.update(rf)
    # 客户端只在上「收到」的字节上推进解密器（与 tgproxy.py L286 一致，绝不在自己发出的首包上推进）
    cl = dec.update(cf)
    cp, cu = _pms_tg_parse_abridged_frame(cl)
    assert cp is not None and struct.unpack('<I', cp[8:12])[0] == _PMS_MTPROTO_RES_PQ and cp[12:28] == nonce, f'{name}: client cannot parse resPQ'
    print(f'  {name}: form={s.form} domain={s.domain!r} -> handshake OK (req_pq+resPQ verified both directions)')
    ok_all = True

# 再测一次 secret 解析的边界：裸 16B hex / 非法 / 空
edge = [
    ('raw16 hex', '00112233445566778899aabbccddeeff', 'raw', True),
    ('empty', '', '', False),
    ('bad len', 'aabbcc', '', False),
    ('unknown', 'ff00112233445566778899aabbccddeeff00', '', False),
]
for label, sec, expect_form, expect_ok in edge:
    r = pms_tg_parse_secret(sec)
    status = 'OK' if (r.ok == expect_ok and (not expect_ok or r.form == expect_form)) else 'MISMATCH'
    print(f'  edge {label}: form={r.form!r} ok={r.ok} err={r.error!r} -> {status}')
    if status == 'MISMATCH':
        ok_all = False

print('ALL HANDSHAKE + PARSE TESTS PASSED' if ok_all else 'SOME TESTS FAILED')
