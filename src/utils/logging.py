from logging import getLogger


def get_logger(name: str, slice: int | None = None):
    names = name.split(".")
    if not names:
        return getLogger()
    if names[0] == "src":
        names[0] = "app"
    if slice:
        names = names[:slice]
    lg = getLogger(names[0])
    for n in names[1:]:
        lg = lg.getChild(n)
    return lg
