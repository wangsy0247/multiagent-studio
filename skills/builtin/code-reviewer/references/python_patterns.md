# Python Anti-Patterns Reference

Common Python-specific mistakes to flag during code review.

## 1. Mutable Default Arguments

```python
# ❌  BAD — the same list is shared across all calls
def append_to_list(item, target=[]):
    target.append(item)
    return target

# ✅  GOOD
def append_to_list(item, target=None):
    if target is None:
        target = []
    target.append(item)
    return target
```

## 2. Broad Exception Handling

```python
# ❌  BAD — catches KeyboardInterrupt, SystemExit, etc.
try:
    do_something()
except:
    pass

# ✅  GOOD — catch specific exceptions
try:
    do_something()
except (ValueError, IOError) as exc:
    logger.warning("Failed: %s", exc)
```

## 3. Using `is` for Value Comparison

```python
# ❌  BAD — `is` checks identity, not equality
if x is 1000:
    ...

# ✅  GOOD
if x == 1000:
    ...
```

## 4. Iterating Over Dict Keys

```python
# ❌  BAD
for key in d.keys():
    print(key, d[key])

# ✅  GOOD
for key, value in d.items():
    print(key, value)
```

## 5. String Concatenation in Loops

```python
# ❌  BAD — O(n²) string creation
result = ""
for item in items:
    result += str(item)

# ✅  GOOD — join
result = "".join(str(item) for item in items)
```

## 6. Bare `assert` for Production Validation

```python
# ❌  BAD — assertions are stripped with `python -O`
assert user.is_admin, "Must be admin"

# ✅  GOOD — explicit check
if not user.is_admin:
    raise PermissionError("Must be admin")
```

## 7. `type()` Instead of `isinstance()`

```python
# ❌  BAD — doesn't handle subclasses
if type(obj) == MyClass:
    ...

# ✅  GOOD
if isinstance(obj, MyClass):
    ...
```

## 8. Late-binding Closure Trap

```python
# ❌  BAD — all lambdas capture the same `i`
funcs = [lambda: i for i in range(5)]

# ✅  GOOD — bind at creation time
funcs = [lambda i=i: i for i in range(5)]
```

## 9. Missing Context Manager for Resources

```python
# ❌  BAD — file may not be closed on exception
f = open("data.txt")
data = f.read()
f.close()

# ✅  GOOD
with open("data.txt") as f:
    data = f.read()
```

## 10. `import *`

```python
# ❌  BAD — pollutes namespace, unclear origins
from module import *

# ✅  GOOD
from module import specific_func, AnotherClass
```
