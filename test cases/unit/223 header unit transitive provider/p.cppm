export module p;

import q;

export constexpr int p_view = q_view;

export int pval() { return qval(); }
