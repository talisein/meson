import <vector>;
import modlib;

int main() {
    std::vector<int> v{1, 2, 3};
    return (mval() == 42 && v.size() == 3) ? 0 : 1;
}
