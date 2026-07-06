import "declared.h";    // declared in cpp_header_units
import "undeclared.h";  // NOT declared -> collator must reject the build

int main() {
    return decl_val() + undecl_val();
}
