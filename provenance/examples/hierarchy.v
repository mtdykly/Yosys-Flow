module leaf(input a, b, c, output y);
    wire t;
    assign t = a & b;
    assign y = t ^ c;
endmodule

module branch(input a, b, c, output y);
    leaf inner(.a(a), .b(b), .c(c), .y(y));
endmodule

module demo(input a, b, c, d, e, f, output y, z);
    branch u0(.a(a), .b(b), .c(c), .y(y));
    branch u1(.a(d), .b(e), .c(f), .y(z));
endmodule
