int square_entry(int x);

// Actually call into the modules-using C++ library, so a green run proves the
// C target linked the module objects rather than merely configuring.
int main(void)
{
    return square_entry(5) == 25 ? 0 : 1;
}
