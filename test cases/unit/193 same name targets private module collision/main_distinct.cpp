int util1_value();
int other_value();

int main() {
    return (util1_value() == 1 && other_value() == 3) ? 0 : 1;
}
