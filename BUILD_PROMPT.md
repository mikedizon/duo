# Build: Python application for macOS (Python 3.11+, stdlib only)

Build a production-quality, standard-library-only Python 3.11+ application for macOS, including Apple Silicon.
This brief defines general engineering requirements. Application-specific functionality must be specified separately.

## Engineering requirements

- Use only the Python standard library; no external dependencies or `requirements.txt` are needed.
- Keep application logic separate from input/output so it can be tested independently.
- Validate inputs and handle malformed data explicitly.
- Provide clear error messages and structured `key=value` diagnostic logs on stderr.
- Never log secrets.
- Handle SIGINT and SIGTERM gracefully, releasing resources before exit.

## Documentation

- Include a `README.md` describing prerequisites, setup, usage, and how to run tests.
- Document supported behavior, configuration, and known limitations.

## Tests

- Use `unittest` and standard-library mocks.
- Cover supported behavior, invalid inputs, error handling, and resource cleanup.
- Mock external interactions; tests must not require live network access or launch external applications.
- Keep tests deterministic and isolated from user data.
