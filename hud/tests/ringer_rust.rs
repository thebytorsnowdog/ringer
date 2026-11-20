#![cfg(target_os = "linux")]

use glib::{Bytes, Variant};

#[test]
fn reads_serialized_string_arrays_in_both_directions() {
    // Existing GLib `as` wire format: three NUL-terminated strings and their end offsets.
    let bytes = Bytes::from_static(b"first\0caf\xc3\xa9\0last\0\x06\x0c\x11");
    let variant = Variant::from_bytes::<Vec<String>>(&bytes);
    let mut strings = variant.array_iter_str().unwrap();

    assert_eq!(strings.len(), 3);
    assert_eq!(strings.next(), Some("first"));
    assert_eq!(strings.next_back(), Some("last"));
    assert_eq!(strings.next(), Some("café"));
    assert_eq!(strings.next_back(), None);

    assert_eq!(variant.array_iter_str().unwrap().nth(1), Some("café"));
    assert_eq!(variant.array_iter_str().unwrap().nth_back(1), Some("café"));
    assert_eq!(variant.array_iter_str().unwrap().last(), Some("last"));
}

#[test]
fn reads_an_empty_string_array() {
    let bytes = Bytes::from_static(b"");
    let variant = Variant::from_bytes::<Vec<String>>(&bytes);
    let mut strings = variant.array_iter_str().unwrap();

    assert_eq!(strings.len(), 0);
    assert_eq!(strings.next(), None);
    assert_eq!(strings.next_back(), None);
}
