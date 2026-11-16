from requests.sessions import Session

s = Session()
s.request("GET")


def scoped():
    local = Session()
    local.request("POST")


def cross_scope():
    s.request("DELETE")
