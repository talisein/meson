import mathmod;

// A C-linkage entry from the linked Fortran library.
extern "C" int fhelper(int);

int main() {
  if (square(5) != 25) {
    return 1;
  }
  if (fhelper(41) != 42) {
    return 2;
  }
  return 0;
}
