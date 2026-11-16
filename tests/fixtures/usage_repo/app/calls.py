from requests.sessions import Session

Session(timeout=1, verify=False)
Session(1)
Session(**{"timeout": 1})
Session(1, *extra)
