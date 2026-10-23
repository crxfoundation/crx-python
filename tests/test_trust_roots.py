"""The CA bundle that requests uses does not trust emSign ECC Root CA - G3."""

import hashlib
import re
import ssl

import certifi
import requests.certs

# SHA-256 of the DER form of emSign ECC Root CA - G3 (eMudhra). Its intermediate keys are public.
EMSIGN_ECC_G3_SHA256 = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"
PEM = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)


def fingerprints(path):
    with open(path, encoding="ascii") as f:
        return {hashlib.sha256(ssl.PEM_cert_to_DER_cert(m.group(0))).hexdigest() for m in PEM.finditer(f.read())}


def test_bundle_has_no_emsign_ecc_g3():
    assert requests.certs.where() == certifi.where()
    fps = fingerprints(requests.certs.where())
    assert len(fps) > 100
    assert EMSIGN_ECC_G3_SHA256 not in fps
