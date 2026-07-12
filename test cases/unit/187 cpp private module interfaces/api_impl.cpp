module api;

// One TU importing its own target's private modules (detail, hidden) and a
// linked dependency's public module (pub): on GCC this is the one mapper
// file that must mix --private-bmi-dir and --bmi-dir entries.
import detail;
import hidden;
import pub;

int api_value() {
    return detail_value() + hidden_value() + pub_value();
}
