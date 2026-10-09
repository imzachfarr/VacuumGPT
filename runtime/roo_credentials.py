"""Secrets use the macOS Keychain backend directly, never files or CLI arguments."""
import json
import sys
from roo_config import SERVICE

def backend():
    if sys.platform!='darwin':raise RuntimeError('MacOSRequired')
    from keyring.backends.macOS import Keyring
    return Keyring()

def credentials():
    value=backend().get_password(SERVICE,'owner-login')
    if not value:raise RuntimeError('RunInstallerToConnectAccount')
    data=json.loads(value)
    return data['email'],data['password'],data['country']

def store(email,password,country):
    backend().set_password(SERVICE,'owner-login',json.dumps(dict(email=email,password=password,country=country)))

def delete():
    try:backend().delete_password(SERVICE,'owner-login')
    except __import__('keyring').errors.PasswordDeleteError:pass
