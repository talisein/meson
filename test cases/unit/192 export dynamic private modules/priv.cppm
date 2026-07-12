export module priv;

// Private: only the executable's own translation units may import this, and
// its BMI must never reach the shared cache -- even though the executable is
// linkable and does publish pub from the very same target.
export int priv_value() {
    return 22;
}
