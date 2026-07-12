// mode == 'own-collision': the build fails at this executable's own collator,
// which sees its own private "detail" and the one its linked library provides
// privately.
import detail;

int util1_value();

int main() {
    return (detail_value() == 4 && util1_value() == 1) ? 0 : 1;
}
