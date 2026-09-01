# Contributing to Strip Cash Bot

Thank you for your interest in contributing! Here's how to get started.

## Code of Conduct

Be respectful, inclusive, and constructive in all interactions.

## Getting Started

1. **Fork the repository**
2. **Clone your fork**
   ```bash
   git clone https://github.com/YOUR_USERNAME/mod3.git
   cd mod3
   ```

3. **Create a branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

## Development Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Setup .env
cp .env.example .env
# Add your DISCORD_TOKEN
```

## Making Changes

### Code Style
- Use **Black** for formatting: `black .`
- Follow **PEP 8** guidelines
- Use **Flake8** for linting: `flake8 .`

### Testing
- Write tests for new features
- Run tests: `pytest tests/ -v`
- Ensure 80%+ code coverage
- Tests must pass before PR submission

### Commit Messages
- Use clear, descriptive messages
- Start with verb: "Add", "Fix", "Update", "Remove"
- Example: `Add earnings alert feature` or `Add partner referral links`

## Submitting Changes

1. **Push to your fork**
   ```bash
   git push origin feature/your-feature-name
   ```

2. **Create a Pull Request**
   - Provide clear description
   - Reference related issues (#123)
   - Ensure all tests pass

3. **PR Requirements**
   - [ ] Tests written and passing
   - [ ] Code formatted with Black
   - [ ] Linting passes (Flake8)
   - [ ] Documentation updated
   - [ ] No merge conflicts

## Types of Contributions

### Bug Reports
- Use GitHub Issues
- Provide minimal reproduction case
- Include Python version and OS

### Feature Requests
- Open an Issue with `[FEATURE]` prefix
- Describe use case
- Suggest implementation if possible

### Documentation
- Update README.md
- Add docstrings to functions
- Include examples

### Code
- Follow existing patterns
- Keep functions small and focused
- Add type hints when possible

## Testing Guidelines

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=. --cov-report=html

# Run specific test
pytest tests/test_bot.py::TestAffiliateLinks::test_affiliate_links_exist -v
```

## Questions?

- Open an Issue with `[QUESTION]` prefix
- Check existing issues first
- Visit: https://github.com/jdkdlfhbdjdj-dot/mod3

---

Thank you for contributing! 💰
