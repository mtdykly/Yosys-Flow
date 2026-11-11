"""Evaluate dependency generators without using the Python call stack."""


def evaluate_stack(root):
    """A frame yields a child frame and receives its return value.

    Frames own their memoization and active-path checks. Closing them in reverse
    order on failure also runs their finally blocks, so a rejected cycle or
    unsupported node cannot poison a later query's active set.
    """
    stack = [root]
    result = None
    try:
        while stack:
            try:
                child = stack[-1].send(result)
            except StopIteration as done:
                stack.pop()
                result = done.value
            else:
                stack.append(child)
                result = None
        return result
    finally:
        while stack:
            stack.pop().close()
