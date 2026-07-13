import <vector>;

#ifndef PRELUDE_TOKEN
#error "the forced include did not reach this TU"
#endif

int main() {
    std::vector<int> v{PRELUDE_TOKEN, 2, 3};
    return v.size() == 3 ? 0 : 1;
}
