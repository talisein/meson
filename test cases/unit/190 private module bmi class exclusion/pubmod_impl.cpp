module pubmod;

import priv;

int pubmod_value() {
    return priv_value();
}
