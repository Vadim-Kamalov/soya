class BusinessError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def fail(code, message):
    raise BusinessError(code, message)

