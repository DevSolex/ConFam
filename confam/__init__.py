"""
confam — shared domain package.

Contains database connection helpers, domain models, and business logic
shared across services. Services (settlement-engine, checkout) import from
here; they do not share code with each other directly.
"""
